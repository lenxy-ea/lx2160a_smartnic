#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


UPSTREAM_ANCHORS = {
    "boot-pbl": 0x000000,
    "fip": 0x100000,
    "env-primary": 0x500000,
    "secure-headers": 0x600000,
    "ddr-phy-fip": 0x800000,
    "fuse-provisioning-capsule": 0x880000,
    "cpld-firmware": 0x900000,
    "platform-reserved-a": 0x940000,
    "platform-reserved-b": 0x980000,
    "manifest-a": 0x9C0000,
    "mc-firmware": 0xA00000,
    "dpl": 0xD00000,
    "dpc": 0xE00000,
    "linux-dtb": 0xF00000,
}


def number(value, field):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as error:
            raise SystemExit(f"invalid {field}: {value}") from error
    raise SystemExit(f"invalid {field} type: {type(value).__name__}")


def fail(message):
    raise SystemExit(f"FAIL: {message}")


def main():
    parser = argparse.ArgumentParser(description="Validate the X200 Flash layout")
    parser.add_argument(
        "layout",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("flash-layout-v1.json"),
    )
    args = parser.parse_args()

    data = json.loads(args.layout.read_text(encoding="utf-8"))
    capacity = number(data["capacity"], "capacity")
    alignment = number(data["layout_alignment"], "layout_alignment")
    erase_domain = number(
        data["erase_policy"]["update_erase_domain"],
        "erase_policy.update_erase_domain",
    )
    addressing = data["addressing"]

    if capacity != 0x01000000:
        fail(f"capacity is 0x{capacity:x}, expected one 16 MB chip (0x01000000)")
    if addressing.get("mode") != "independent-bank-local":
        fail("addressing mode must be independent-bank-local")
    if addressing.get("concatenated") is not False:
        fail("D11 and D12 must not be concatenated")
    if alignment != 0x10000 or erase_domain != 0x10000:
        fail("layout alignment and update erase domain must both be 64 KB")
    if data["erase_policy"].get("exact_part_and_jedec_id_required_before_write") is not True:
        fail("exact Flash identification must be required before writes")
    if [bank.get("id") for bank in addressing.get("banks", [])] != ["D11", "D12"]:
        fail("banks must be exactly D11 and D12")
    for bank in addressing["banks"]:
        if number(bank["capacity"], f"{bank['id']} capacity") != capacity:
            fail(f"{bank['id']} does not have the layout capacity")

    regions = data["regions"]
    names = [region["name"] for region in regions]
    if len(names) != len(set(names)):
        fail("region names are not unique")

    cursor = 0
    for region in regions:
        name = region["name"]
        offset = number(region["offset"], f"{name}.offset")
        size = number(region["size"], f"{name}.size")
        payload_max = number(region["payload_max_size"], f"{name}.payload_max_size")
        end = offset + size

        if offset != cursor:
            relation = "gap" if offset > cursor else "overlap"
            fail(f"{relation} before {name}: cursor=0x{cursor:06x}, offset=0x{offset:06x}")
        if offset % alignment or size % alignment:
            fail(f"{name} is not aligned to 0x{alignment:x}")
        if size <= 0 or payload_max < 0 or payload_max > size:
            fail(f"invalid size contract for {name}")
        if end > capacity:
            fail(f"{name} ends beyond the Flash capacity")

        print(
            f"0x{offset:06x}-0x{end - 1:06x}  0x{size:06x}  "
            f"{region['update_class']:<22} {name}"
        )
        cursor = end

    if cursor != capacity:
        fail(f"layout ends at 0x{cursor:x}, expected 0x{capacity:x}")

    by_name = {region["name"]: region for region in regions}
    for name, expected_offset in UPSTREAM_ANCHORS.items():
        if name not in by_name:
            fail(f"missing upstream anchor region {name}")
        actual_offset = number(by_name[name]["offset"], f"{name}.offset")
        if actual_offset != expected_offset:
            fail(
                f"{name} offset is 0x{actual_offset:x}, "
                f"expected upstream anchor 0x{expected_offset:x}"
            )

    identity = data.get("identity_policy", {})
    if "identity_storage" in data:
        fail("the retired factory identity storage contract must be removed")
    if identity.get("mac_authority") != "x200-soc-derived-mac-v1":
        fail("MAC identity must use the SoC UID-derived authority")
    if identity.get("evidence_ref") != "../hardware-evidence-v1.json#/facts/x200-soc-derived-mac-v1":
        fail("MAC identity must cite the SoC UID-derived hardware evidence fact")
    if identity.get("board_eeprom_storage") is not False:
        fail("board EEPROM must not store BSP content")
    if identity.get("manufacturing_metadata_status") != "unimplemented":
        fail("manufacturing metadata has no implemented storage contract")

    env_policy = data["environment_policy"]
    expected_env = {
        "env-primary": number(env_policy["primary_offset"], "environment primary offset"),
        "env-redundant": number(
            env_policy["redundant_offset"], "environment redundant offset"
        ),
    }
    if number(env_policy["environment_size"], "environment size") != erase_domain:
        fail("U-Boot environment size must be exactly one update erase domain")
    if number(env_policy["sector_size"], "environment sector size") != erase_domain:
        fail("U-Boot environment sector size must match the update erase domain")
    for name, offset in expected_env.items():
        region = by_name[name]
        if number(region["offset"], f"{name}.offset") != offset:
            fail(f"{name} has the wrong offset")
        if number(region["size"], f"{name}.size") != erase_domain:
            fail(f"{name} must occupy exactly one update erase domain")

    manifest_names = ["manifest-a", "manifest-b", "update-journal", "manifest-future"]
    for index, name in enumerate(manifest_names):
        region = by_name[name]
        expected_offset = 0x9C0000 + index * erase_domain
        if number(region["offset"], f"{name}.offset") != expected_offset:
            fail(f"{name} has the wrong offset")
        if number(region["size"], f"{name}.size") != erase_domain:
            fail(f"{name} must occupy exactly one update erase domain")

    dtb = by_name["linux-dtb"]
    if number(dtb["offset"], "linux-dtb.offset") != 0xF00000:
        fail("Linux DTB must start at the NXP 0xF00000 anchor")
    if number(dtb["size"], "linux-dtb.size") != 0x100000:
        fail("Linux DTB must retain the complete 1 MB NXP erase domain")
    if "board-data" in by_name:
        fail("board-data must not share the NOR DTB erase domain")

    pbl = by_name["boot-pbl"]["internal_layout"]
    rcw_pbi_offset = number(pbl["rcw_pbi_offset"], "boot-pbl.rcw_pbi_offset")
    bl2_offset = number(pbl["bl2_offset"], "boot-pbl.bl2_offset")
    flexspi_base = number(pbl["flexspi_window_base"], "boot-pbl.flexspi_window_base")
    blockcopy_source = number(pbl["blockcopy_source"], "boot-pbl.blockcopy_source")
    if rcw_pbi_offset != 0 or bl2_offset != 0x9000:
        fail("PBL format v1 must use RCW/PBI@0 and BL2@0x9000")
    if blockcopy_source != flexspi_base + bl2_offset:
        fail("PBI blockcopy source does not match the BL2 Flash offset")
    if pbl.get("blockcopy_length") != "derived-from-bl2.bin":
        fail("PBI blockcopy length must be derived from the built BL2 payload")

    manifest_path = args.layout.with_name(data["manifest_contract"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if number(manifest["slot_size"], "manifest.slot_size") != erase_domain:
        fail("manifest slots must each occupy one update erase domain")
    if manifest["slots"] != ["manifest-a", "manifest-b"]:
        fail("manifest contract must bind manifest A and B")
    profile_components = data["profile_contract"]["bound_components"]
    if profile_components != ["rcw", "dpc", "dpl", "dtb"]:
        fail("layout has the wrong board-profile component set")
    if manifest["profile_bound_components"] != profile_components:
        fail("manifest and layout disagree on board-profile components")
    if manifest["profile_component_field"] != data["profile_contract"]["manifest_field"]:
        fail("manifest and layout disagree on the board-profile field")
    if manifest["selection"].get("select") != "highest-valid-sequence":
        fail("manifest selection must use the highest valid sequence")
    if manifest.get("slot_encoding", {}).get("type") != "x200-json-sha256-v1":
        fail("manifest slots must use the X200 JSON/SHA-256 v1 framing")
    if manifest["selection"].get("equal_sequence_tie_break") != "lowest-offset-valid-slot":
        fail("equal manifest sequences must select the lowest-offset valid slot")
    if data["environment_policy"].get("mutable_environment_must_not_bypass_secure_boot") is not True:
        fail("mutable U-Boot environment must not bypass secure boot")
    fuse_policy = data["fuse_provisioning_policy"]
    if fuse_policy.get("normal_image_state") != "erased":
        fail("ordinary firmware images must leave the fuse capsule erased")
    if fuse_policy.get("private_keys_forbidden") is not True:
        fail("private keys must be forbidden from the fuse capsule")
    if fuse_policy.get("plaintext_secrets_forbidden") is not True:
        fail("plaintext secrets must be forbidden from the fuse capsule")

    print()
    print("PASS: D11 and D12 each use one independent 0x01000000-byte layout")
    print("PASS: regions are contiguous, non-overlapping, aligned, and within capacity")
    print("PASS: FlexBuild LX2160 anchor offsets are preserved")
    print("PASS: MAC identity uses the SoC UID; board EEPROM storage is disabled")
    print("PASS: manufacturing metadata is unimplemented; the full 1 MB DTB domain is intact")
    print("PASS: redundant env and manifest copies use separate 64 KB erase domains")
    print("PASS: RCW/PBI, BL2 blockcopy, and board-profile contracts are consistent")


if __name__ == "__main__":
    main()
