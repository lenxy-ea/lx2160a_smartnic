SHELL := /bin/bash
REPO_ROOT := $(abspath ../..)
PYTHON ?= python3
JOBS ?= 4
BUILDER ?= localhost/x200-toolchain:1
FIRMWARE_INPUTS ?= $(REPO_ROOT)/build/lx2160-x200/firmware-inputs
FIRMWARE_OUT ?= $(REPO_ROOT)/build/firmware
KERNEL_OUT ?= $(REPO_ROOT)/build/kernel
KERNEL_MANIFEST ?= $(KERNEL_OUT)/manifest.json
RUNTIME_OUT ?= $(REPO_ROOT)/build/runtime
RUNTIME_ROOT ?= $(REPO_ROOT)/build/runtime-root
MANUAL_RATE_OUT ?= $(REPO_ROOT)/build/manual-rate
OS_OUT ?= $(REPO_ROOT)/build/os
CONFIG ?=
AUTHORIZED_KEY ?=
OPERATOR ?= x200

.PHONY: help check test toolchain fetch firmware kernel manual-rate runtime bootstrap rootfs sata

help:
	@echo 'check test toolchain fetch firmware kernel manual-rate runtime bootstrap rootfs sata'
	@echo 'Build outputs must be fresh. runtime requires CONFIG; rootfs accepts AUTHORIZED_KEY.'

check:
	$(PYTHON) $(REPO_ROOT)/tools/check_source.py
	$(PYTHON) firmware/check_current.py
	$(PYTHON) profiles/validate_profile.py
	$(PYTHON) flash-layout/validate-layout.py

test:
	$(PYTHON) $(REPO_ROOT)/tools/run_tests.py

toolchain:
	podman build -t "$(BUILDER)" -f firmware/Containerfile .

fetch:
	$(PYTHON) firmware/fetch_inputs.py --inputs-dir "$(FIRMWARE_INPUTS)"

firmware: fetch
	$(PYTHON) firmware/build.py --inputs-dir "$(FIRMWARE_INPUTS)" --output "$(FIRMWARE_OUT)" --jobs "$(JOBS)"

kernel:
	$(PYTHON) linux/build.py --builder "$(BUILDER)" --out-dir "$(KERNEL_OUT)" --jobs "$(JOBS)"

manual-rate:
	@test -n "$(CONFIG)" || { echo 'CONFIG must name an explicit site configuration' >&2; exit 1; }
	$(PYTHON) dual-rate/manual-rate/build_manual_rate.py --kernel-manifest "$(KERNEL_MANIFEST)" --artifact-root "$(REPO_ROOT)" --source "$(KERNEL_OUT)/linux" --kernel-output "$(KERNEL_OUT)/kernel-output" --config "$(CONFIG)" --out "$(MANUAL_RATE_OUT)"

runtime:
	@test -n "$(CONFIG)" || { echo 'CONFIG must name an explicit site configuration' >&2; exit 1; }
	$(PYTHON) pcie-management/persistent/package_card.py --kernel-manifest "$(KERNEL_MANIFEST)" --artifact-root "$(REPO_ROOT)" --config "$(CONFIG)" --out "$(RUNTIME_OUT)" --rootfs "$(RUNTIME_ROOT)" --manual-rate "$(MANUAL_RATE_OUT)"

bootstrap:
	$(PYTHON) os/build.py bootstrap --builder "$(BUILDER)" --out-dir "$(OS_OUT)"

rootfs:
	$(PYTHON) os/build.py rootfs --builder "$(BUILDER)" --kernel-manifest "$(KERNEL_MANIFEST)" --out-dir "$(OS_OUT)" --runtime-root "$(RUNTIME_ROOT)" --runtime-manifest "$(RUNTIME_OUT)/overlay-manifest.json" --user "$(OPERATOR)" $(if $(AUTHORIZED_KEY),--authorized-key "$(AUTHORIZED_KEY)",)

sata:
	$(PYTHON) os/build.py sata --builder "$(BUILDER)" --kernel-manifest "$(KERNEL_MANIFEST)" --out-dir "$(OS_OUT)"
