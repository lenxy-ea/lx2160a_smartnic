# Configuration, installation and upgrades

Build packages first using the [build guide](build.md). Use the same site
configuration for the card package and host installation. The addresses and
PCI locations in `config.example.json` must be replaced with your own values.
Do not copy another system's keys or device identities. Run card administration
commands as root or through sudo.

## Host

The maintained host service requires systemd, NetworkManager, firewalld,
nftables, Python 3, kmod and matching kernel headers/native NTB modules.
Its supplied early-loading configuration uses dracut. Hosts without those
components need a deliberately integrated host service configuration.

The following stages the verified host package into an **offline host root**:

```sh
python3 bsp/lx2160-x200/pcie-management/persistent/host_install.py \
  --bundle "$PWD/build/host-vntb/bundle" \
  --manifest-sha256 VERIFIED_HOST_MANIFEST_SHA256 \
  --config "$PWD/config.local.json" --root /mnt/host-root
```

Use the `manifest_sha256` emitted by the host builder. The installer verifies
the package, stages modules/configuration and refuses conflicting existing
files. It does not start services or load drivers. Review the staged files,
rebuild that host's initramfs and explicitly enable the service according to
the host's maintenance procedure. Do not unload a driver while the peer owns
active mappings; use the coordinated quiescence tool for planned shutdowns.

## Card and SATA image

The rootfs build integrates the matching card service and modules and can
provision an operator SSH public key. Root and password-based SSH login remain
disabled. Missing SSH host keys are generated on the card's first boot, not
distributed in the image.

The SATA output is a complete regular GPT disk image. Before a separate
physical installation, identify the intended SATA device, confirm its capacity
against the image manifest, and use an appropriate image-writing procedure.
Building the image is independent of that destructive installation operation.

The boot files, kernel and card service are a matching set. The board's NOR
profile must match the SATA boot script; the script stops if the identity
differs. Do not install a standalone kernel from a different build over the
packaged modules and runtime identity.

## Kernel and card-service upgrade

Create a package from the producer manifest and matching card bundle:

```sh
python3 bsp/lx2160-x200/pcie-management/kernel-upgrade/package.py \
  --kernel-manifest "$PWD/build/kernel/manifest.json" --artifact-root "$PWD" \
  --card-bundle "$PWD/build/runtime/card" --card-sha256 VERIFIED_CARD_BUNDLE_SHA256 \
  --out "$PWD/build/kernel-upgrade"
```

The resulting package contains its installer and a checksummed inventory.
Stage it on the card only during an explicitly planned update:

```sh
python3 /path/to/kernel-upgrade/install.py stage \
  --root / --package /path/to/kernel-upgrade \
  --manifest-sha256 VERIFIED_UPGRADE_MANIFEST_SHA256 \
  --current-image-sha256 VERIFIED_CURRENT_IMAGE_SHA256 \
  --expected-boot-id CURRENT_CARD_BOOT_ID \
  --journal /var/lib/x200/updates/UNIQUE_UPDATE_ID
```

The installer checks package identity, current Image bytes and boot identity,
then journals the changed SATA files. It neither reboots nor writes NOR.
Keep the journal until next-boot validation succeeds. `status` inspects it;
`rollback` restores only that transaction after checking for foreign changes.
The transaction is not a guarantee of atomic recovery from power loss.

## Boot firmware upgrade

`bringup/package_boot_update.py` combines a fresh bank image and its pack receipt
with an actual current NOR snapshot and SATA boot script:

```sh
python3 bsp/lx2160-x200/bringup/package_boot_update.py \
  --current-snapshot /path/to/current-nor.bin \
  --candidate-image /path/to/new-bank-image.bin \
  --current-boot /path/to/current-boot.scr \
  --candidate-boot "$PWD/build/firmware/boot.scr" \
  --producer-manifest /path/to/new-bank-pack-receipt.json \
  --config "$PWD/config.local.json" --out "$PWD/build/boot-update"
```

The packager preserves both environment slots byte for byte and rejects other
protected-region changes. Configure the device, bank, sysfs and filesystem
identities in `boot_update`; review the changed sectors and package digest.

On the card, `boot_update.py prepare --bundle PATH --manifest-sha256 DIGEST`
checks the device and stores a transaction snapshot. `boot_update.py apply`
with the same arguments performs the actual write after same-boot and exact
current-byte checks. It reads back each sector, writes manifest sectors last,
verifies the complete image and stages the matching SATA `boot.scr`. It does
not reboot or switch banks. Keep the transaction files locally. The procedure
is not power-loss atomic; best-effort restoration applies only to this transaction.

## Daily observation

The rootfs includes `x200-network-diagnostics`. For an explicit interface
selection, the source collector reads standard Linux interfaces:

```sh
python3 bsp/lx2160-x200/network-probe/collect.py \
  --interface eth0 --output /tmp/x200-network.json
```

Collected output may contain your network and device identities. Keep it local
unless you have reviewed it for a specific support request. Runtime changes
such as port-rate selection are explicit commands and are separate from
read-only collection. The runtime overlay also installs the matching manual-rate
module and `x200-port-rate` wrapper. `x200-port-rate status` observes the ports;
`x200-port-rate set eth0 10000` requests runtime 10G for that datapath port.
Supported rates are 10000 and 25000 Mbit/s; transition/cable combinations need
their own qualification. This does not change initial boot settings.
