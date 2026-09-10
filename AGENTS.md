# LX2160A X200 SmartNIC

- This repository owns the maintained X200 BSP and system tools.
- Include only maintained source, configuration, tests and product documentation. Do not add private research, original device images, captured logs, credentials or deployment records.
- Every hardware-sensitive board parameter must bind to the public hardware contract at `bsp/lx2160-x200/hardware-evidence-v1.json`. Generic NXP sources do not establish X200 board facts.
- Build from pinned public dependencies and tracked configuration. Packages validate actual producer outputs, never historical receipts.
- Preserve SoC UID MAC authority, NOR environment and SATA configuration. Board EEPROM is not BSP storage.
- Use a single current boot path: RCW/PBI, TF-A, U-Boot, Linux and Debian. Remove superseded implementations.
- Build and package commands must not access or write target hardware. Deployment requires an explicit operator invocation.
- Keep generated products outside Git. Review staged content and commit intentional changes atomically.
- Preserve upstream copyright and license notices. Independent original tools and documentation use MIT unless marked otherwise.
