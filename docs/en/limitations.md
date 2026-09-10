# Validation and limitations

- The maintained configuration has Linux startup validation with PCIe3
  disconnected. A Gen3 x4 configuration ceiling is not a negotiated link
  measurement. Connected-device speed, width and throughput remain untested.
- A successful source build validates the build and packaging interfaces.
  It does not qualify the newly generated binaries on hardware or establish
  production reliability.
- Warm-start and reset reliability require further hardware qualification;
  a previously observed secondary-CPU startup stall is unresolved.
- Builds use the actual build time by default. Controlled reproduction of
  time-dependent components requires matching source, dependency pins,
  toolchain and `SOURCE_DATE_EPOCH`. Full Debian rootfs byte reproducibility
  is not claimed; installed package versions are recorded.
- NOR hashes and manifests provide integrity checks, not authenticated boot.
- Board EEPROM is not a configuration or identity store.
- MC and DDR training firmware are external binary dependencies. Their source
  and licensing are separate from this project's original tools.
- Target installation, Flash writes, device restart and PCIe qualification
  are not performed by the build commands or CI.
