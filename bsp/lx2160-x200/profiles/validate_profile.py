#!/usr/bin/env python3
"""Compile and cross-check one X200 board profile without target access."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
BOARD_DIR = SCRIPT_DIR.parent
REPO_ROOT = BOARD_DIR.parent.parent
DEFAULT_PROFILE = SCRIPT_DIR / "x200-s1_12-s2_05-s3_02-v1"
DEFAULT_RCW_TREE = REPO_ROOT / "build/vendor/nxp-qoriq-rcw-lf-6.12.49-2.2.0"
DEFAULT_LINUX_TREE = REPO_ROOT / "build/vendor/linux-imx-lf-6.12.49-2.2.0-shallow"
DEFAULT_OUT = REPO_ROOT / "build/lx2160-x200/native18-profile"
SDK_LOCK_FILE = BOARD_DIR / "flexbuild/sdk-source-lock.json"


class ProfileError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ProfileError(f"expected a JSON object: {path}")
    return value


def verify_sdk_lock(lock: dict[str, Any]) -> None:
    if lock.get("schema_version") != 1:
        raise ProfileError("SDK source lock must use schema version 1")
    if lock.get("purpose") != "Board-neutral pinned NXP Debian SDK inputs":
        raise ProfileError("profile validation requires the board-neutral SDK lock")
    for forbidden in ("machine", "artifact_sets", "reference_input_sha256"):
        if forbidden in lock:
            raise ProfileError(f"SDK source lock contains board-specific field: {forbidden}")


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise ProfileError(f"required host executable is missing: {name}")
    return executable


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    stdout_path: Path | None = None,
) -> str:
    if stdout_path is None:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = result.stdout
    else:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                text=True,
                stdout=stream,
                stderr=subprocess.PIPE,
            )
        output = result.stderr
    if result.returncode:
        rendered = " ".join(command)
        raise ProfileError(
            f"command failed ({result.returncode}): {rendered}\n{output.rstrip()}"
        )
    return output.strip()


def git_head(tree: Path) -> str:
    if not (tree / ".git").exists():
        raise ProfileError(f"not a Git working tree: {tree}")
    return run(["git", "rev-parse", "HEAD"], cwd=tree)


def source_paths(profile_dir: Path, profile: dict[str, Any]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for component in ("rcw", "dpc", "dpl", "dtb"):
        relative = profile.get("sources", {}).get(component)
        if not isinstance(relative, str) or not relative:
            raise ProfileError(f"profile source is missing: {component}")
        path = (profile_dir / relative).resolve()
        if profile_dir.resolve() not in path.parents:
            raise ProfileError(f"profile source escapes its directory: {relative}")
        if not path.is_file():
            raise ProfileError(f"profile source does not exist: {path}")
        paths[component] = path
    return paths


def load_evidence_registry(
    profile_dir: Path, profile: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    relative = profile.get("evidence_registry")
    if not isinstance(relative, str) or not relative:
        raise ProfileError("profile must name its hardware evidence registry")
    path = (profile_dir / relative).resolve()
    if path != (BOARD_DIR / "hardware-evidence-v1.json").resolve():
        raise ProfileError(f"profile selected an unexpected evidence registry: {path}")
    registry = read_json(path)
    if registry.get("schema_version") != 1 or registry.get("board") != "lx2160-x200":
        raise ProfileError("hardware evidence registry has an unexpected identity")
    return path, registry


def verify_evidence_contract(profile, registry):
    facts = registry.get("facts", {})
    bindings = profile.get("evidence_bindings", {})
    if not facts or not bindings:
        raise ProfileError("hardware fact contract is empty")
    for component, ids in bindings.items():
        if not ids:
            raise ProfileError("empty hardware binding: " + component)
        for fact_id in ids:
            fact = facts.get(fact_id)
            if (not isinstance(fact, dict) or
                    fact.get("authority") not in {"target-measurement-or-observation", "factory-image-or-board-binary", "explicit-smartnic-design-delta"} or
                    fact.get("implementation_use") not in {"allowed-baseline", "allowed-with-target-gate", "allowed-design-delta"}):
                raise ProfileError("invalid hardware authority: " + fact_id)
    expected_rcw = facts.get("x200-rcw-configuration", {}).get("value", {})
    profile_dir = SCRIPT_DIR / profile["board_profile_id"]
    rcw = source_paths(profile_dir, profile)["rcw"].read_text()
    actual_rcw = {key: int(value, 0) for key, value in re.findall(r"^([A-Z0-9_]+)=(0x[0-9a-f]+|[0-9]+)$", rcw, re.M)}
    if actual_rcw != expected_rcw:
        raise ProfileError("RCW values differ from the bound hardware configuration")
    verify_rx_auto_contract(profile, facts)
    return {fact: facts[fact]["authority"] for ids in bindings.values() for fact in ids}


def verify_rx_auto_contract(profile: dict[str, Any], facts: dict[str, Any]) -> None:
    startup_id = "x200-rx-auto-startup-default"
    if profile.get("evidence_bindings", {}).get("serdes1_rx_gain") != [startup_id, "x200-dual-rate-native18-design"]:
        raise ProfileError("automatic RX requires the separately gated startup fact")
    design = facts.get("x200-dual-rate-native18-design", {})
    clock = design.get("value", {}).get("clock_selection", {})
    if (design.get("implementation_use") != "allowed-with-target-gate" or
            clock.get("SRDS_PRTCL_S1") != 18 or clock.get("MEM_PLL_RAT") != 32 or
            clock.get("MEM2_PLL_RAT") != 32 or
            profile.get("serdes", {}).get("s1", {}).get("rx_auto_lanes") != [4, 5]):
        raise ProfileError("native18 RX lane/clock design mismatch")
    startup = facts.get(startup_id, {})
    if startup.get("implementation_use") != "allowed-with-target-gate":
        raise ProfileError("automatic RX startup fact has an unexpected implementation scope")
    if profile.get("serdes", {}).get("s1", {}).get("rx_gain_k2") != "automatic":
        raise ProfileError("RX profile must select automatic K2")
    value = startup.get("value", {})
    if (value.get("k2_mode") != "automatic" or
            value.get("k2_lanes4_to7") != ["automatic"] * 4 or
            value.get("expected_recr0_lanes4_to7") != ["0x00000085"] * 4):
        raise ProfileError("RX automatic profile differs from startup design fact")


def verify_profile_contract(
    profile: dict[str, Any], paths: dict[str, Path]
) -> str:
    profile_id = profile.get("board_profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        raise ProfileError("board_profile_id must be a non-empty string")
    if profile.get("status") != "development":
        raise ProfileError("X200 profile status must be development")
    if profile.get("deployment_allowed") is not False:
        raise ProfileError("development must keep deployment_allowed=false")

    gates = profile.get("deployment_gates")
    if not isinstance(gates, list) or not gates:
        raise ProfileError("at least one deployment gate is required")
    link_gate = next((gate for gate in gates if gate.get("id") == "serdes2-gen3-target"), {})
    if link_gate.get("state") != "open" or profile.get("target_qualified") is not False:
        raise ProfileError("S2 Gen3 configuration must keep target qualification open")
    allowed_gate_states = {"open", "partial", "passed"}
    for gate in gates:
        if not isinstance(gate, dict) or gate.get("state") not in allowed_gate_states:
            raise ProfileError(f"invalid deployment gate: {gate}")
        if gate["state"] != "open" and not gate.get("evidence"):
            raise ProfileError(
                f"non-open deployment gate lacks evidence: {gate.get('id')}"
            )

    for component, path in paths.items():
        source = path.read_text(encoding="utf-8")
        if profile_id not in source:
            raise ProfileError(f"{component} source does not carry {profile_id}")

    serdes = profile.get("serdes", {})
    expected = {"s1": 18, "s2": 5, "s3": 2}
    for name, value in expected.items():
        actual = serdes.get(name, {}).get("source_value")
        if actual != value:
            raise ProfileError(f"profile {name} is {actual}; expected {value}")

    if profile.get("ddr_mt_s") != 3200 or profile.get("reliability_qualified") is not False:
        raise ProfileError("native18 must select DDR3200 without reliability qualification")
    expected_ports = [{"dpmac": mac, "interface": "DPMAC_ETH_IF_XFI" if mac < 5 else "DPMAC_ETH_IF_25G_AUI",
                       "link_type": "MAC_LINK_TYPE_PHY"} for mac in range(3, 7)]
    if profile.get("ethernet", {}).get("ports") != expected_ports:
        raise ProfileError("native18 must use PHY-owned MAC3/4 XFI and MAC5/6 CAUI")
    if profile.get("pcie") != [
        {"controller": 3, "mode": "root-complex", "lanes": 4, "max_link_speed": 3},
        {"controller": 4, "mode": "disabled", "lanes": 0},
        {"controller": 5, "mode": "endpoint", "lanes": 8},
        {"controller": 6, "mode": "disabled", "lanes": 0},
    ]:
        raise ProfileError("profile must describe PCIe3 x4 Gen3 RC, PCIe5 x8 endpoint, PCIe4/6 disabled")

    constraints = profile.get("build_constraints", {})
    if constraints.get("pcie5_x8_pbi_contract") != "x200-pex5-ep-v1":
        raise ProfileError("profile must select the X200 board PCIe5 x8 PBI contract")
    if constraints.get("pcie6_ccsr_access_allowed") is not False:
        raise ProfileError("profile must prohibit PCIe6 CCSR access")

    flash = profile.get("flash", {})
    if (
        flash.get("bank_count") != 2
        or flash.get("bank_capacity") != "0x01000000"
        or flash.get("addressing") != "independent-bank-local"
        or flash.get("concatenated") is not False
    ):
        raise ProfileError("profile must describe two independent 16 MB Flash banks")
    return profile_id


def assignment(source: str, name: str) -> int:
    match = re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*([0-9A-Fa-fx]+)\s*$", source)
    if match is None:
        raise ProfileError(f"RCW assignment is missing: {name}")
    return int(match.group(1), 0)


def assignment_or_zero(source: str, name: str) -> int:
    match = re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*([0-9A-Fa-fx]+)\s*$", source)
    return int(match.group(1), 0) if match is not None else 0


def verify_pcie5_x8_pbi(source: str, label: str) -> None:
    """Enforce the board PCIe5 x8 pre-boot register contract."""

    writes = [
        (int(address, 0), int(value, 0), line.strip())
        for line in source.splitlines()
        if (
            match := re.match(
                r"^\s*write\s+(0x[0-9a-f]+)\s*,\s*(0x[0-9a-f]+)\s*$",
                line,
                flags=re.IGNORECASE,
            )
        )
        for address, value in (match.groups(),)
    ]
    required_writes = (
        (0x03800098, 0x00000000),
        (0x038008BC, 0x00000001),
        (0x03800000, 0x80C01957),
        (0x03800180, 0x00000010),
    )
    for address, value in required_writes:
        count = sum(
            item_address == address and item_value == value
            for item_address, item_value, _line in writes
        )
        if count != 1:
            raise ProfileError(
                f"{label} must contain exactly one write "
                f"0x{address:08x},0x{value:08x}; found {count}"
            )

    if any(0x03700000 <= address < 0x03800000 for address, _value, _line in writes):
        raise ProfileError(f"{label} accesses absent PCIe4 in S2 protocol5")

    pcie5_writes = [
        (address, value)
        for address, value, _line in writes
        if 0x03800000 <= address < 0x03900000
    ]
    if pcie5_writes != list(required_writes):
        rendered = "; ".join(
            f"0x{address:08x},0x{value:08x}" for address, value in pcie5_writes
        )
        raise ProfileError(
            f"{label} PCIe5 PBI must be the ordered four-write board "
            f"sequence; found: {rendered}"
        )

    pcie6_accesses = [
        line
        for address, _value, line in writes
        if 0x03900000 <= address < 0x03A00000
    ]
    if pcie6_accesses:
        rendered = "; ".join(pcie6_accesses[:4])
        raise ProfileError(
            f"{label} accesses disabled PCIe6 CCSR space 0x039xxxxx: {rendered}"
        )


def verify_native18_rcw(text: str) -> dict[str, int]:
    expected_assignments = {
        # Canonical functionally accepted DDR3200/native18 clock selection.
        # hardware-evidence-v1.json: x200-dual-rate-native18-design.
        "MEM_PLL_CFG": 3,
        "MEM_PLL_RAT": 32,
        "MEM2_PLL_CFG": 3,
        "MEM2_PLL_RAT": 32,
        "SRDS_PRTCL_S1": 18,
        "SRDS_PLL_PD_PLL1": 0,
        "SRDS_PLL_PD_PLL2": 0,
        "SRDS_REFCLKF_DIS_S1": 0,
        "SRDS_PLL_REF_CLK_SEL_S1": 2,
        "SRDS_INTRA_REF_CLK_S1": 1,
        "SRDS_PRTCL_S2": 5,
        "SRDS_DIV_PEX_S2": 1,
        "SRDS_PLL_PD_PLL3": 0,
        "SRDS_PLL_PD_PLL4": 0,
        "SRDS_PLL_REF_CLK_SEL_S2": 0,
        "SRDS_INTRA_REF_CLK_S2": 0,
        "SRDS_PRTCL_S3": 2,
        "HOST_AGT_PEX5": 1,
        "SRDS_PLL_PD_PLL5": 1,
        "SRDS_REFCLKF_DIS_S3": 1,
        "BOOT_LOC": 26,
    }
    for name, value in expected_assignments.items():
        actual = assignment_or_zero(text, name)
        if actual != value:
            raise ProfileError(f"RCW {name}={actual}; expected {value}")
    for name in ("HOST_AGT_PEX6", "SRDS_PLL_PD_PLL6"):
        actual = assignment_or_zero(text, name)
        if actual != 0:
            raise ProfileError(f"RCW {name}={actual}; expected 0")
    if re.search(r"(?m)^\s*PBI_LENGTH\s*=", text):
        raise ProfileError("RCW must let rcw.py derive PBI_LENGTH")
    if re.search(r"(?mi)^\s*blockcopy\s+0x0*f\s*,", text):
        raise ProfileError("RCW must not contain a fixed terminal BL2 blockcopy")

    lanes = re.findall(r"#include\s*<\.\./lx2160asi/25g_eq_s1_lane_([a-h])\.rcw>", text)
    if lanes != ["f", "e"]:
        raise ProfileError("native18 must retain 25G EQ only on lanes F/E")
    pex3 = re.findall(r"#include\s*<\.\./lx2160asi/(a00[0-9]+_PEX3\.rcw)>", text)
    if pex3 != ["a009531_PEX3.rcw", "a008851_PEX3.rcw"] or "_PEX4.rcw>" in text:
        raise ProfileError("S2 configuration requires PCIe3 Gen3 errata only; PCIe4 is absent")
    return expected_assignments


def compile_rcw(
    source: Path,
    rcw_tree: Path,
    out_dir: Path,
    expected_commit: str | None,
    *, archive_proof: dict[str, Any] | None = None,
) -> tuple[Path, Path, str]:
    if expected_commit is None:
        if not archive_proof:
            raise ProfileError("archive RCW build requires checksum proof")
        archive = Path(archive_proof["path"])
        actual_commit = archive_proof.get("commit")
        pinned = read_json(SDK_LOCK_FILE)["components"]["rcw"]["commit"]
        if (actual_commit != pinned or not archive.is_file() or
                sha256(archive) != archive_proof.get("sha256")):
            raise ProfileError("RCW archive checksum/commit proof mismatch")
        expected_commit = pinned
    else:
        actual_commit = git_head(rcw_tree)
    if actual_commit != expected_commit:
        raise ProfileError(
            f"RCW tree is at {actual_commit}; expected pinned {expected_commit}"
        )
    rcw_tool = rcw_tree / "rcw.py"
    rcwi = rcw_tree / "lx2160asi/lx2160a.rcwi"
    working_dir = rcw_tree / "lx2160asi"
    if not rcw_tool.is_file() or not rcwi.is_file() or not working_dir.is_dir():
        raise ProfileError(f"incomplete pinned RCW tree: {rcw_tree}")

    text = source.read_text(encoding="utf-8")
    expected_assignments = verify_native18_rcw(text)

    preprocessed = run(
        ["gcc", "-E", "-x", "c", "-P", "-I", ".", str(source)],
        cwd=working_dir,
    )
    if re.search(r"(?mi)^\s*blockcopy\s+0x0*f\s*,\s*0x20009000\s*,", preprocessed):
        raise ProfileError("preprocessed RCW contains a stale BL2 blockcopy")
    verify_pcie5_x8_pbi(preprocessed, "preprocessed RCW/PBI")

    binary = out_dir / "rcw/lx2160-x200.bin"
    binary.parent.mkdir(parents=True, exist_ok=True)
    output = run(
        [sys.executable, str(rcw_tool), "-w", "-i", str(source), "-o", str(binary)],
        cwd=working_dir,
    )
    if "Error:" in output:
        raise ProfileError(f"rcw.py reported an error:\n{output}")
    data = binary.read_bytes()
    if len(data) >= 0x9000:
        raise ProfileError(f"RCW/PBI output overlaps BL2 at 0x9000: {len(data)} bytes")
    # lx2160a.rcwi selects little-endian 64-bit word swapping, so the on-media
    # byte order of the PBL magic is 55 aa 55 aa.
    if data[:4] != bytes.fromhex("55aa55aa"):
        raise ProfileError("RCW output has an unexpected PBL preamble")

    audit = out_dir / "rcw/lx2160-x200.audit.rcw"
    reverse_output = run(
        [
            sys.executable,
            str(rcw_tool),
            "-r",
            "--rcwi",
            str(rcwi),
            "-I",
            str(rcwi.parent),
            "-i",
            str(binary),
            "-o",
            str(audit),
        ],
        cwd=working_dir,
    )
    if "Error:" in reverse_output:
        raise ProfileError(f"rcw.py reverse audit reported an error:\n{reverse_output}")
    audit_text = audit.read_text(encoding="utf-8")
    for name, value in expected_assignments.items():
        actual = assignment_or_zero(audit_text, name)
        if actual != value:
            raise ProfileError(f"compiled RCW {name}={actual}; expected {value}")
    for name in ("HOST_AGT_PEX6", "SRDS_PLL_PD_PLL6"):
        actual = assignment_or_zero(audit_text, name)
        if actual != 0:
            raise ProfileError(f"compiled RCW {name}={actual}; expected 0")
    verify_pcie5_x8_pbi(audit_text, "compiled RCW/PBI reverse audit")
    return binary, audit, actual_commit


def compile_plain_dts(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(destination), str(source)])


def compile_linux_dts(source: Path, linux_tree: Path, destination: Path) -> None:
    dts_dir = linux_tree / "arch/arm64/boot/dts/freescale"
    include_dir = linux_tree / "include"
    if not (dts_dir / "fsl-lx2160a.dtsi").is_file():
        raise ProfileError(f"pinned Linux DTS inputs are missing from {linux_tree}")
    preprocessed = destination.with_suffix(".preprocessed.dts")
    run(
        [
            "gcc",
            "-E",
            "-nostdinc",
            "-undef",
            "-D__DTS__",
            "-x",
            "assembler-with-cpp",
            "-I",
            str(dts_dir),
            "-I",
            str(include_dir),
            str(source),
        ],
        stdout_path=preprocessed,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(
        ["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(destination), str(preprocessed)]
    )


def fdt_string(dtb: Path, node: str, prop: str) -> str:
    return run(["fdtget", "-t", "s", str(dtb), node, prop])


def fdt_int(dtb: Path, node: str, prop: str) -> list[int]:
    value = run(["fdtget", "-t", "u", str(dtb), node, prop])
    return [int(item, 0) for item in value.split()]


def require_fdt_string(dtb: Path, node: str, prop: str, expected: str) -> None:
    actual = fdt_string(dtb, node, prop)
    if actual != expected:
        raise ProfileError(f"{dtb.name}:{node}:{prop}={actual!r}; expected {expected!r}")


def require_no_fdt_property(dtb: Path, node: str, prop: str) -> None:
    result = subprocess.run(
        ["fdtget", str(dtb), node, prop],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        raise ProfileError(f"forbidden DT property is present: {node}:{prop}")


def require_no_fdt_node(dtb: Path, node: str) -> None:
    result = subprocess.run(
        ["fdtget", "-l", str(dtb), node],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        raise ProfileError(f"forbidden DT node is present: {node}")


def verify_dpl_object_inventory(dpl: Path) -> dict[str, Any]:
    """Require definitions, container assignments and connections to agree.

    DPL object names use decimal IDs (dpio@10 is MC DPIO 0x000a),
    while obj_set ids are numeric cells. Compare normalized identities.
    """
    def children(node: str) -> list[str]:
        return run(["fdtget", "-l", str(dpl), node]).splitlines()

    def identity(name: str) -> tuple[str, int]:
        match = re.fullmatch(r"([a-z][a-z0-9]*)@([0-9]+)", name)
        if not match:
            raise ProfileError(f"invalid DPL object identity: {name}")
        return match[1], int(match[2], 10)

    def names(objects: set[tuple[str, int]]) -> str:
        return ", ".join(f"{kind}@{number}" for kind, number in sorted(objects))

    definitions: set[tuple[str, int]] = set()
    for node in children("/objects"):
        obj = identity(node)
        if obj in definitions:
            raise ProfileError(f"DPL object defined more than once: {names({obj})}")
        definitions.add(obj)
    assignments: set[tuple[str, int]] = set()
    for container in children("/containers"):
        identity(container)
        base = f"/containers/{container}/objects"
        for node in children(base):
            path = f"{base}/{node}"
            if node.startswith("obj_set@"):
                kind = fdt_string(dpl, path, "type")
                if re.fullmatch(r"[a-z][a-z0-9]*", kind) is None:
                    raise ProfileError(f"invalid DPL object type: {kind}")
                objects = [(kind, number) for number in fdt_int(dpl, path, "ids")]
            elif node.startswith("obj@"):
                objects = [identity(fdt_string(dpl, path, "obj_name"))]
            else:
                raise ProfileError(f"unknown DPL assignment node: {path}")
            for obj in objects:
                if obj in assignments:
                    raise ProfileError(f"DPL object assigned more than once: {names({obj})}")
                assignments.add(obj)
    missing = assignments - definitions
    orphan = definitions - assignments
    if missing or orphan:
        raise ProfileError(f"DPL inventory mismatch: missing definitions [{names(missing)}]; "
                           f"unassigned definitions [{names(orphan)}]")
    connections = children("/connections")
    for node in connections:
        for prop in ("endpoint1", "endpoint2"):
            endpoint = identity(fdt_string(dpl, f"/connections/{node}", prop))
            if endpoint not in assignments:
                raise ProfileError(f"DPL connection {node} references unassigned {names({endpoint})}")
    by_type: dict[str, int] = {}
    for kind, _number in sorted(definitions):
        by_type[kind] = by_type.get(kind, 0) + 1
    return {"object_count": len(definitions), "by_type": by_type, "connections": len(connections)}


def verify_serdes2_linux_tree(linux: Path) -> None:
    rc = "/soc/pcie@3600000"
    require_fdt_string(linux, rc, "compatible", "fsl,ls2088a-pcie")
    require_fdt_string(linux, rc, "reg-names", "regs config")
    require_fdt_string(linux, rc, "status", "okay")
    for prop, expected in (("num-lanes", [4]), ("max-link-speed", [3]),
                           ("linux,pci-domain", [0]),
                           ("reg", [0, 0x03600000, 0, 0x100000,
                                    0x90, 0, 0, 0x2000])):
        if fdt_int(linux, rc, prop) != expected:
            raise ProfileError(f"PCIe3 configuration {prop} mismatch")
    for prop in ("apio-wins", "ppio-wins"):
        require_no_fdt_property(linux, rc, prop)
    for controller in range(4):
        require_fdt_string(linux, f"/soc/sata@{0x3200000 + controller * 0x10000:x}",
                           "status", "okay" if controller == 0 else "disabled")


def verify_compiled_trees(
    profile_id: str, dpc: Path, dpl: Path, linux: Path
) -> None:
    verify_dpl_object_inventory(dpl)
    profile_prop = "rhinelab,board-profile-id"
    for dtb in (dpc, dpl, linux):
        require_fdt_string(dtb, "/", profile_prop, profile_id)

    for mac in range(3, 7):
        node = f"/board_info/ports/mac@{mac}"
        require_fdt_string(dpc, node, "enet_if", "DPMAC_ETH_IF_XFI" if mac < 5 else "DPMAC_ETH_IF_25G_AUI")
        require_fdt_string(dpc, node, "link_type", "MAC_LINK_TYPE_PHY")
        require_fdt_string(dpc, node, "pcs_autoneg", "on")
        require_fdt_string(dpc, node, "fec_mode", "none")
        endpoint = f"/soc/fsl-mc@80c000000/dpmacs/ethernet@{mac}"
        require_fdt_string(linux, endpoint, "status", "okay")
        require_fdt_string(linux, endpoint, "phy-mode", "10gbase-r" if mac < 5 else "25gbase-r")
        require_fdt_string(linux, endpoint, "managed", "in-band-status")
        if fdt_int(linux, endpoint, "phys") != fdt_int(linux, "/soc/phy@1ea0000", "phandle") + [10 - mac]:
            raise ProfileError(f"MAC{mac} has incorrect SerDes PHY lane")
        mdio = f"/soc/mdio@{0x8c03000 + mac * 0x4000:x}"
        if fdt_int(linux, endpoint, "pcs-handle") != fdt_int(linux, mdio + "/ethernet-phy@0", "phandle"):
            raise ProfileError(f"MAC{mac} has incorrect PCS mapping")
        require_fdt_string(linux, mdio, "status", "okay")
        if fdt_int(linux, mdio, "clock-frequency") != [2500000]:
            raise ProfileError(f"{mdio} must use the X200 2.5MHz MDIO setting")

    require_fdt_string(linux, "/aliases", "crypto", "/soc/crypto@8000000")

    mapping = {
        1: ("dpni@0", "dpmac@6"),
        2: ("dpni@1", "dpmac@5"),
        3: ("dpni@2", "dpmac@3"),
        4: ("dpni@3", "dpmac@4"),
    }
    for connection, endpoints in mapping.items():
        node = f"/connections/connection@{connection}"
        require_fdt_string(dpl, node, "endpoint1", endpoints[0])
        require_fdt_string(dpl, node, "endpoint2", endpoints[1])

    require_fdt_string(
        linux, "/soc/spi@20c0000/flash@0", "compatible", "jedec,spi-nor"
    )
    require_fdt_string(
        linux, "/soc/spi@20c0000/flash@1", "compatible", "jedec,spi-nor"
    )
    require_no_fdt_property(linux, "/soc/spi@20c0000", "nxp,fspi-has-second-chip")
    require_no_fdt_node(linux, "/soc/i2c@2030000/eeprom@51")
    require_fdt_string(
        linux, "/soc/i2c@2030000/rtc@68", "compatible", "st,m41t11"
    )
    ep = "/soc/pcie-ep@3800000"
    require_fdt_string(linux, ep, "compatible", "fsl,lx2160ar2-pcie-ep")
    require_fdt_string(linux, ep, "status", "okay")
    if fdt_int(linux, ep, "num-lanes") != [8]:
        raise ProfileError(f"{ep} must have eight lanes")
    interrupts = fdt_int(linux, ep, "interrupts")
    if len(interrupts) < 2 or interrupts[1] != 128:
        raise ProfileError(f"{ep} has unexpected interrupt cells: {interrupts}")
    verify_serdes2_linux_tree(linux)
    for controller in (1, 2, 4, 6):
        require_fdt_string(linux, f"/soc/pcie@{0x3400000 + (controller - 1) * 0x100000:x}", "status", "disabled")
    require_no_fdt_node(linux, "/soc/pcie-ep@3900000")
    require_fdt_string(linux, "/soc/pcie@3800000", "status", "disabled")
    require_fdt_string(linux, "/soc/pcie@3900000", "status", "disabled")
    reservations = {
        "/reserved-memory/pcie-ep-memory@94000000": [
            0,
            0x94000000,
            0,
            0x10000,
        ],
    }
    for node, expected_reg in reservations.items():
        actual_reg = fdt_int(linux, node, "reg")
        if actual_reg != expected_reg:
            raise ProfileError(
                f"{linux.name}:{node}:reg={actual_reg!r}; "
                f"expected {expected_reg!r}"
            )
        run(["fdtget", str(linux), node, "no-map"])
    require_no_fdt_node(linux, "/reserved-memory/pcie-ep-guard@94010000")


def tool_version(command: list[str]) -> str:
    return run(command).splitlines()[0]









if __name__ == "__main__":
    profile = read_json(DEFAULT_PROFILE / "profile.json")
    paths = source_paths(DEFAULT_PROFILE, profile)
    verify_profile_contract(profile, paths)
    verify_evidence_contract(profile, read_json(BOARD_DIR / "hardware-evidence-v1.json"))
    verify_native18_rcw(paths["rcw"].read_text())
    print("profile contract: PASS")
