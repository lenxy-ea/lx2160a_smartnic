# Architecture

The X200 boot chain is PBL → RCW/PBI → TF-A BL2 → TF-A BL31 → native
U-Boot → Linux → Debian. Board configuration is maintained as source; pinned
NXP dependencies provide generic SoC implementation and tools.

## Hardware configuration

The public hardware contract is
`bsp/lx2160-x200/hardware-evidence-v1.json`. It identifies the board facts
used by RCW/PBI, TF-A, U-Boot, DPC, DPL and the Linux device tree. A profile
must reference the corresponding facts. Configuration validation checks these
bindings; it does not constitute a physical measurement.

Only the maintained board profile is included. It uses DDR3200 and the
SerDes2 protocol 5 configuration. PCIe3 is configured with a Gen3 x4 ceiling;
actual link negotiation with a connected device is not qualified.

## Storage and boot ownership

The two NOR devices are independently addressed 16 MiB devices. Each image
has its own layout and integrity metadata. They are not concatenated into one
Flash address space. U-Boot verifies the selected composition before booting.
Checksums detect corruption; they do not provide authenticated boot.

TF-A owns DDR initialization. U-Boot owns the fixed SATA boot policy and
loads the bank-local MC firmware, DPC, DPL and device tree. SATA partition 1
contains boot files and the kernel; partition 2 contains Debian. A build or
packaging command produces regular files, never writes a physical disk.

NOR environment slots retain runtime boot preferences. Board MAC addresses
derive from the SoC UID. System configuration belongs on SATA. Board EEPROM
is not used for BSP content, configuration or identity; DDR SPD and SFP
identification are separate interfaces.

## PCIe management and networking

Linux publishes the PCIe5 management endpoint after completing controller
initialization. Matching host and card modules and services establish the
management network. The host's interface selection, addressing, routing,
DNS and optional NAT are operator configuration.

The build producer owns artifact identity. Packages consume its manifest
and validate the actual kernel and modules. Build IDs, file hashes, module
vermagic and imported symbol CRCs prevent mixing unrelated outputs.

NetworkManager owns system network configuration. Runtime services manage
their specific PCIe publication lifecycle and preserve the required shutdown
ordering. Installation and target activation are explicit operator actions.

## Development boundary

This repository contains product source, tests, dependency pins and product
documentation. It does not require external board research archives to build.
Generated output stays under `build/`. New hardware-sensitive settings require
an explicit contract update; reference-board defaults cannot supply X200 facts.
