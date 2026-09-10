#!/usr/bin/env python3
"""Build and verify one independent 16 MB X200 development Flash image.

This tool only writes a regular file. It contains no programmer, MTD, SPI or
target-device operation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
BOARD_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BOARD_DIR))
from build_time import build_environment
REPO_ROOT = BOARD_DIR.parent.parent
DEFAULT_LAYOUT = SCRIPT_DIR / "flash-layout-v1.json"
DEFAULT_CONTRACT = SCRIPT_DIR / "firmware-manifest-contract-v1.json"
DEFAULT_LOCK = BOARD_DIR / "flexbuild/sdk-source-lock.json"
CURRENT = json.loads((BOARD_DIR / "firmware/current.json").read_text(encoding="utf-8"))
DEFAULT_PROFILE_RECEIPT = (
    REPO_ROOT / "build/lx2160-x200/firmware-current/profile-receipt.json"
)
DEFAULT_BOOT_RECEIPT = (
    REPO_ROOT
    / "build/lx2160-x200/firmware-current/boot-receipt.json"
)
PROFILE_ID = CURRENT["board_profile_id"]
BOOT_PHASE = CURRENT["boot_phase"]
FIRMWARE_VARIANT = CURRENT["firmware_variant"]
SLOT_HEADER = struct.Struct("<8sII")
SLOT_MAGIC = b"X200FW1\0"
SHA256_SIZE = 32


class PackError(RuntimeError):
    pass


def number(value: Any, field: str) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as error:
            raise PackError(f"invalid {field}: {value}") from error
    raise PackError(f"invalid {field} type: {type(value).__name__}")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise PackError(f"expected a JSON object: {path}")
    return value


def verify_sdk_lock(lock: dict[str, Any]) -> None:
    if lock.get("schema_version") != 1:
        raise PackError("SDK source lock must use schema version 1")
    if lock.get("purpose") != "Board-neutral pinned NXP Debian SDK inputs":
        raise PackError("X200 packer requires the board-neutral SDK source lock")
    for forbidden in ("machine", "artifact_sets", "reference_input_sha256"):
        if forbidden in lock:
            raise PackError(f"SDK source lock contains board-specific field: {forbidden}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_data(path: Path, label: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PackError(f"cannot read {label} {path}: {error}") from error
    if not data:
        raise PackError(f"{label} is empty: {path}")
    return data


def git_commit() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if status.returncode:
        raise PackError("cannot inspect BSP Git status")
    if status.stdout.strip():
        raise PackError("BSP Git worktree must be clean before packing an image")
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.returncode:
        raise PackError("cannot determine the BSP Git commit")
    return process.stdout.strip()


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)


def region_map(layout: dict[str, Any]) -> dict[str, dict[str, Any]]:
    regions = layout.get("regions")
    if not isinstance(regions, list):
        raise PackError("layout regions are missing")
    result = {region["name"]: region for region in regions}
    if len(result) != len(regions):
        raise PackError("layout region names are not unique")
    return result


def component(
    *,
    version: str,
    offset: int,
    data: bytes,
    board_profile_id: str | None = None,
    container: str | None = None,
    scope: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "version": version,
        "offset": f"0x{offset:06x}",
        "size": len(data),
        "sha256": sha256_bytes(data),
    }
    if board_profile_id is not None:
        result["board_profile_id"] = board_profile_id
    if container is not None:
        result["container"] = container
    if scope is not None:
        result["scope"] = scope
    if extra:
        result.update(extra)
    return result


def encode_manifest_slot(manifest: dict[str, Any], slot_size: int) -> bytes:
    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    header = SLOT_HEADER.pack(SLOT_MAGIC, manifest["format_version"], len(payload))
    digest = hashlib.sha256(payload).digest()
    used = len(header) + len(payload) + len(digest)
    if used > slot_size:
        raise PackError(f"manifest needs {used} bytes, slot has {slot_size}")
    return header + payload + digest + bytes([0xFF]) * (slot_size - used)


def decode_manifest_slot(slot: bytes) -> dict[str, Any]:
    if len(slot) < SLOT_HEADER.size + SHA256_SIZE:
        raise PackError("manifest slot is too short")
    magic, version, payload_size = SLOT_HEADER.unpack_from(slot)
    if magic != SLOT_MAGIC:
        raise PackError(f"manifest slot has invalid magic {magic!r}")
    payload_start = SLOT_HEADER.size
    payload_end = payload_start + payload_size
    digest_end = payload_end + SHA256_SIZE
    if digest_end > len(slot):
        raise PackError("manifest payload exceeds its slot")
    payload = slot[payload_start:payload_end]
    expected = slot[payload_end:digest_end]
    if hashlib.sha256(payload).digest() != expected:
        raise PackError("manifest payload checksum mismatch")
    if any(byte != 0xFF for byte in slot[digest_end:]):
        raise PackError("manifest slot padding is not erased")
    manifest = json.loads(payload.decode("ascii"))
    if manifest.get("format_version") != version:
        raise PackError("manifest header and payload versions differ")
    return manifest


def put_region(
    image: bytearray,
    regions: dict[str, dict[str, Any]],
    name: str,
    payload: bytes,
) -> None:
    if name not in regions:
        raise PackError(f"layout region is missing: {name}")
    region = regions[name]
    offset = number(region["offset"], f"{name}.offset")
    size = number(region["size"], f"{name}.size")
    payload_max = number(region["payload_max_size"], f"{name}.payload_max_size")
    if len(payload) > size or len(payload) > payload_max:
        raise PackError(
            f"{name} payload is {len(payload)} bytes; limit is {min(size, payload_max)}"
        )
    if any(byte != 0xFF for byte in image[offset : offset + size]):
        raise PackError(f"attempt to overwrite non-erased region: {name}")
    image[offset : offset + len(payload)] = payload


def verify_input_receipts(
    data: dict[str, bytes],
    profile_receipt_path: Path,
    boot_receipt_path: Path,
) -> None:
    profile = read_json(profile_receipt_path)
    if profile.get("validation") != "PASS" or profile.get("board_profile_id") != PROFILE_ID:
        raise PackError("profile validation receipt is not a passing X200 profile")
    boot = read_json(boot_receipt_path)
    profile_names = {"dpc": "dpc", "dpl": "dpl", "dtb": "dtb"}
    for data_name, receipt_name in profile_names.items():
        expected = profile["artifacts"][receipt_name]
        if len(data[data_name]) != expected["size"] or sha256_bytes(data[data_name]) != expected["sha256"]:
            raise PackError(f"{data_name} does not match the profile validation receipt")
    rcw_record = profile["artifacts"]["rcw_pbi"]
    rcw_path = Path(rcw_record["path"])
    if not rcw_path.is_absolute():
        rcw_path = REPO_ROOT / rcw_path
    rcw_pbi = file_data(rcw_path, "profile RCW/PBI")
    if len(rcw_pbi) != rcw_record["size"] or sha256_bytes(rcw_pbi) != rcw_record["sha256"]:
        raise PackError("profile RCW/PBI no longer matches its validation receipt")
    if data["pbl"][:40] != rcw_pbi[:40] or data["pbl"][44:136] != rcw_pbi[44:136]:
        raise PackError("PBL RCW bytes differ outside the NXP PBI-length update")
    source_word = struct.unpack_from("<I", rcw_pbi, 40)[0]
    on_media_word = struct.unpack_from("<I", data["pbl"], 40)[0]
    expected_word = ((source_word & 0xFFF00000) + (6 << 20)) | (
        source_word & ~0xFFF00000
    )
    expected_word &= 0xFFFFFFFF
    if on_media_word != expected_word:
        raise PackError("PBL does not contain the canonical NXP PBI-length update")
    stored_checksum = struct.unpack_from("<I", data["pbl"], 136)[0]
    calculated_checksum = sum(struct.unpack("<34I", data["pbl"][:136])) & 0xFFFFFFFF
    if stored_checksum != calculated_checksum:
        raise PackError("PBL RCW checksum is invalid after the PBI-length update")

    if (
        boot.get("validation") != "PASS"
        or boot.get("phase") != BOOT_PHASE
        or boot.get("board_profile_id") != PROFILE_ID
        or boot.get("firmware_variant") != FIRMWARE_VARIANT
        or boot.get("native_uboot_config") != "lx2160x200_tfa_defconfig"
    ):
        raise PackError(
            f"native boot receipt is not a passing X200 Phase {BOOT_PHASE} build"
        )
    for name in ("pbl", "fip", "bl2", "bl31", "uboot", "ddr_phy_fip"):
        data_name = "ddr_phy" if name == "ddr_phy_fip" else name
        expected = boot["artifacts"][name]
        if len(data[data_name]) != expected["size"] or sha256_bytes(data[data_name]) != expected["sha256"]:
            raise PackError(f"{name} does not match the native boot build receipt")


def validate_contract(
    manifest: dict[str, Any], contract: dict[str, Any]
) -> None:
    for field in contract["required_top_level_fields"]:
        if field not in manifest:
            raise PackError(f"manifest field is missing: {field}")
    for field in contract["board_fields"]:
        if field not in manifest["board"]:
            raise PackError(f"manifest board field is missing: {field}")
    for name in contract["required_components"]:
        if name not in manifest["components"]:
            raise PackError(f"manifest component is missing: {name}")
        for field in contract["component_common_fields"]:
            if field not in manifest["components"][name]:
                raise PackError(f"manifest {name}.{field} is missing")
    for name in contract["profile_bound_components"]:
        if manifest["components"][name].get(contract["profile_component_field"]) != manifest["board_profile_id"]:
            raise PackError(f"manifest component {name} has the wrong profile ID")
    for field in contract["build_fields"]:
        if field not in manifest["build"]:
            raise PackError(f"manifest build field is missing: {field}")
    for field in contract["security_fields"]:
        if field not in manifest["security"]:
            raise PackError(f"manifest security field is missing: {field}")
    serialized = json.dumps(manifest, sort_keys=True)
    for forbidden in contract["forbidden_content"]:
        if forbidden in serialized:
            raise PackError(f"manifest contains forbidden field: {forbidden}")


def verify_fip_leaves(data: dict[str, bytes]) -> None:
    """Bind the separately supplied BL31/BL33 receipts to their actual FIP bytes."""
    fip = data["fip"]
    expected = {
        bytes.fromhex("47d4086d4cfe98469b952950cbbd5a00"): data["bl31"],
        bytes.fromhex("d6d0eea7fcead54b97829934f234b6e4"): data["uboot"],
    }
    if len(fip) < 136 or struct.unpack_from("<I", fip)[0] != 0xAA640001:
        raise PackError("invalid native FIP header")
    spans = []
    for index in range(2):
        uuid, offset, size, flags = struct.unpack_from("<16sQQQ", fip, 16 + index * 40)
        leaf = expected.pop(uuid, None)
        if leaf is None or offset < 136 or size != len(leaf) or offset + size > len(fip):
            raise PackError("invalid native FIP leaf or bounds")
        if fip[offset:offset + size] != leaf:
            raise PackError("FIP bytes do not match the BL31/U-Boot build receipt")
        spans.append((offset, offset + size))
    terminator, end, size, flags = struct.unpack_from("<16sQQQ", fip, 96)
    if terminator != bytes(16) or end != len(fip) or size or flags:
        raise PackError("invalid native FIP terminator")
    spans.sort()
    if spans[0][1] > spans[1][0]:
        raise PackError("overlapping native FIP leaves")


def build_image(args: argparse.Namespace) -> tuple[Path, Path]:
    variant = FIRMWARE_VARIANT
    sequence = args.sequence
    if sequence is None:
        sequence = CURRENT["manifest_sequence"]
    layout = read_json(args.layout)
    contract = read_json(args.contract)
    lock = read_json(args.lock)
    verify_sdk_lock(lock)
    lock['build_environment'] = build_environment(lock['build_environment'])
    capacity = number(layout["capacity"], "layout.capacity")
    if capacity != 0x01000000:
        raise PackError("the X200 packer requires one 0x01000000-byte bank")
    banks = [entry["id"] for entry in layout["addressing"]["banks"]]
    if layout["addressing"].get("concatenated") is not False or args.bank not in banks:
        raise PackError("target must be one independent D11 or D12 bank")
    if contract.get("slot_encoding", {}).get("type") != "x200-json-sha256-v1":
        raise PackError("unsupported manifest slot encoding")
    if not 0 <= sequence <= 0xFFFFFFFFFFFFFFFF:
        raise PackError("manifest sequence must be an unsigned 64-bit integer")

    regions = region_map(layout)
    data = {
        "pbl": file_data(args.pbl, "PBL"),
        "fip": file_data(args.fip, "FIP"),
        "bl2": file_data(args.bl2, "BL2"),
        "bl31": file_data(args.bl31, "BL31"),
        "uboot": file_data(args.uboot, "U-Boot"),
        "ddr_phy": file_data(args.ddr_phy_fip, "DDR PHY FIP"),
        "mc": file_data(args.mc, "MC firmware"),
        "dpl": file_data(args.dpl, "DPL"),
        "dpc": file_data(args.dpc, "DPC"),
        "dtb": file_data(args.dtb, "Linux DTB"),
    }
    verify_input_receipts(data, args.profile_receipt, args.boot_receipt)
    verify_fip_leaves(data)
    mc_lock = lock["components"]["mc_bin"]["lx2160a_itb"]
    if sha256_bytes(data["mc"]) != mc_lock["sha256"]:
        raise PackError("MC firmware does not match the pinned 10.40.0 binary")
    bl2_offset = number(
        regions["boot-pbl"]["internal_layout"]["bl2_offset"], "bl2_offset"
    )
    if data["pbl"][:4] != bytes.fromhex("55aa55aa"):
        raise PackError("PBL does not start with the on-media 55aa55aa preamble")
    if data["pbl"][bl2_offset:] != data["bl2"]:
        raise PackError("PBL does not contain the supplied BL2 at 0x9000")

    image = bytearray([number(layout["erased_byte"], "erased_byte")]) * capacity
    payloads = {
        "boot-pbl": data["pbl"],
        "fip": data["fip"],
        "ddr-phy-fip": data["ddr_phy"],
        "mc-firmware": data["mc"],
        "dpl": data["dpl"],
        "dpc": data["dpc"],
        "linux-dtb": data["dtb"],
    }
    for name, payload in payloads.items():
        put_region(image, regions, name, payload)

    components = lock["components"]
    rcw_on_media = data["pbl"][8:136]
    manifest = {
        "magic": contract["magic"],
        "format_version": contract["format_version"],
        "sequence": sequence,
        "image_target": {
            "physical_designator": args.bank,
            "addressing": "independent-bank-local",
            "capacity": capacity,
        },
        "board": {
            "model": "RhineLab LX2160A X200 SmartNIC",
            "board_revision": "unvalidated",
            "compatible_mask": "x200-development-only",
        },
        "board_profile_id": PROFILE_ID,
        "firmware_variant": variant,
        "components": {
            "rcw": component(
                version=components["rcw"]["commit"],
                offset=0,
                data=rcw_on_media,
                board_profile_id=PROFILE_ID,
                scope="on-media-rcw-128",
            ),
            "bl2": component(
                version=components["atf"]["commit"],
                offset=bl2_offset,
                data=data["bl2"],
                container="boot-pbl",
            ),
            "bl31": component(
                version=components["atf"]["commit"],
                offset=number(regions["fip"]["offset"], "fip.offset"),
                data=data["bl31"],
                container="fip",
            ),
            "uboot": component(
                version=components["uboot"]["commit"],
                offset=number(regions["fip"]["offset"], "fip.offset"),
                data=data["uboot"],
                container="fip",
            ),
            "ddr_phy": component(
                version=components["ddr_phy_bin"]["commit"],
                offset=number(regions["ddr-phy-fip"]["offset"], "ddr.offset"),
                data=data["ddr_phy"],
            ),
            "mc": component(
                version=components["mc_bin"]["commit"],
                offset=number(regions["mc-firmware"]["offset"], "mc.offset"),
                data=data["mc"],
                extra={
                    "api_major": mc_lock["api_major"],
                    "api_minor": mc_lock["api_minor"],
                },
            ),
            "dpc": component(
                version=components["mc_utils"]["commit"],
                offset=number(regions["dpc"]["offset"], "dpc.offset"),
                data=data["dpc"],
                board_profile_id=PROFILE_ID,
            ),
            "dpl": component(
                version=components["mc_utils"]["commit"],
                offset=number(regions["dpl"]["offset"], "dpl.offset"),
                data=data["dpl"],
                board_profile_id=PROFILE_ID,
            ),
            "dtb": component(
                version=components["linux"]["commit"],
                offset=number(regions["linux-dtb"]["offset"], "dtb.offset"),
                data=data["dtb"],
                board_profile_id=PROFILE_ID,
            ),
        },
        "containers": {
            "boot-pbl": {
                "offset": regions["boot-pbl"]["offset"],
                "size": len(data["pbl"]),
                "sha256": sha256_bytes(data["pbl"]),
            },
            "fip": {
                "offset": regions["fip"]["offset"],
                "size": len(data["fip"]),
                "sha256": sha256_bytes(data["fip"]),
            },
        },
        "build": {
            "git_commit": git_commit(),
            "timestamp_utc": dt.datetime.fromtimestamp(
                int(lock["build_environment"]["SOURCE_DATE_EPOCH"]), dt.timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "toolchain": "aarch64-linux-gnu-gcc-14.2.0",
            "flexbuild_commit": lock["flexbuild"]["commit"],
            "sdk_source_lock_sha256": sha256_bytes(args.lock.read_bytes()),
            "atf_commit": components["atf"]["commit"],
            "uboot_commit": components["uboot"]["commit"],
            "linux_commit": components["linux"]["commit"],
            "build_user": lock["build_environment"]["KBUILD_BUILD_USER"],
            "build_host": lock["build_environment"]["KBUILD_BUILD_HOST"],
        },
        "security": {
            "secure_boot_policy": "development-sb-en-0",
            "signature_algorithm": "none",
            "signature": "",
        },
    }
    validate_contract(manifest, contract)
    slot_size = number(contract["slot_size"], "manifest.slot_size")
    slot = encode_manifest_slot(manifest, slot_size)
    put_region(image, regions, "manifest-a", slot)
    put_region(image, regions, "manifest-b", slot)

    erased_regions = (
        "env-primary",
        "env-redundant",
        "env-reserved",
        "secure-headers",
        "fuse-provisioning-capsule",
        "cpld-firmware",
        "platform-reserved-a",
        "platform-reserved-b",
        "update-journal",
        "manifest-future",
    )
    for name in erased_regions:
        region = regions[name]
        offset = number(region["offset"], f"{name}.offset")
        size = number(region["size"], f"{name}.size")
        if any(byte != 0xFF for byte in image[offset : offset + size]):
            raise PackError(f"policy requires an erased {name} region")

    for name, payload in payloads.items():
        offset = number(regions[name]["offset"], f"{name}.offset")
        if bytes(image[offset : offset + len(payload)]) != payload:
            raise PackError(f"post-pack verification failed for {name}")
    for name in contract["slots"]:
        offset = number(regions[name]["offset"], f"{name}.offset")
        decoded = decode_manifest_slot(bytes(image[offset : offset + slot_size]))
        if decoded != manifest:
            raise PackError(f"post-pack manifest verification failed for {name}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_path = (
        args.output_dir / f"{PROFILE_ID}-{variant}-{args.bank}.bin"
    )
    receipt_path = (
        args.output_dir
        / f"{PROFILE_ID}-{variant}-{args.bank}-pack-receipt.json"
    )
    for path in (image_path, receipt_path):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise PackError(f"refusing non-regular output path: {path}")
    image_path.write_bytes(image)
    receipt = {
        "schema_version": 1,
        "phase": BOOT_PHASE,
        "operation": "pack-independent-bank",
        "validation": "PASS",
        "deployment_allowed": False,
        "hardware_access_performed": False,
        "source_worktree_clean": True,
        "bank": args.bank,
        "bank_local_capacity": capacity,
        "concatenated": False,
        "layout_id": layout["layout_id"],
        "board_profile_id": PROFILE_ID,
        "firmware_variant": variant,
        "git_commit": manifest["build"]["git_commit"],
        "image": {
            "path": display_path(image_path),
            "size": len(image),
            "sha256": sha256_bytes(image),
        },
        "manifest": {
            "sequence": sequence,
            "slots": contract["slots"],
            "payload_sha256": sha256_bytes(
                json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode("ascii")
            ),
        },
        "erased_regions": list(erased_regions),
        "deployment_scope": "offline source build; target activation is a separate operation",
        "current_version_index": "bsp/lx2160-x200/firmware/current.json",
    }
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return image_path, receipt_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", required=True, choices=("D11", "D12"))
    parser.add_argument("--pbl", required=True, type=Path)
    parser.add_argument("--fip", required=True, type=Path)
    parser.add_argument("--bl2", required=True, type=Path)
    parser.add_argument("--bl31", required=True, type=Path)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--ddr-phy-fip", required=True, type=Path)
    parser.add_argument("--mc", required=True, type=Path)
    parser.add_argument("--dpl", required=True, type=Path)
    parser.add_argument("--dpc", required=True, type=Path)
    parser.add_argument("--dtb", required=True, type=Path)
    parser.add_argument("--sequence", type=int, default=None)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--layout", type=Path, default=DEFAULT_LAYOUT)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument(
        "--profile-receipt", type=Path, default=DEFAULT_PROFILE_RECEIPT
    )
    parser.add_argument("--boot-receipt", type=Path, default=DEFAULT_BOOT_RECEIPT)
    return parser.parse_args()


def main() -> int:
    try:
        image, receipt = build_image(parse_args())
    except (PackError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"X200 Flash pack: FAIL: {error}", file=sys.stderr)
        return 1
    print(f"X200 Flash pack: PASS: {image}")
    print(f"receipt: {receipt}")
    print("deployment_allowed=false; no hardware access was performed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
