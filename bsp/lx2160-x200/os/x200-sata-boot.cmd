# X200 deterministic SATA boot script: x200-s1_12-s2_05-s3_02-v1
echo "X200-BOOT: script=v1 profile=x200-s1_12-s2_05-s3_02-v1"

if test "${board_profile_id}" != "x200-s1_12-s2_05-s3_02-v1"; then
    echo "X200-BOOT: board profile mismatch: ${board_profile_id}"
    exit
fi

# Only a successful cold endpoint probe may authorize Linux publication.
if test "${x200_pcie_boot_policy}" != "linux-publish-only"; then
    echo "X200-BOOT: persistent PCIe requires linux-publish-only NOR policy"
    exit
fi

if run mcinitcmd; then
    echo "X200-BOOT: MC+DPC=PASS"
else
    echo "X200-BOOT: MC+DPC=FAIL"
    exit
fi

if run x200_dpl_stage; then
    echo "X200-BOOT: DPL=STAGED"
else
    echo "X200-BOOT: DPL=FAIL"
    exit
fi

if part uuid scsi ${x200_scsi_dev}:${x200_root_part} x200_root_partuuid; then
    echo "X200-BOOT: root PARTUUID=${x200_root_partuuid}"
else
    echo "X200-BOOT: root partition unavailable"
    exit
fi

if load scsi ${x200_scsi_dev}:${x200_boot_part} ${kernel_addr_r} /Image; then
    echo "X200-BOOT: Linux Image=PASS bytes=${filesize}"
else
    echo "X200-BOOT: Linux Image=FAIL"
    exit
fi

if run x200_fdt_stage; then
    echo "X200-BOOT: DTB=NOR-PASS"
else
    echo "X200-BOOT: DTB=NOR-FAIL"
    exit
fi

fdt addr ${fdt_addr_r}
if fdt get value x200_loaded_profile / rhinelab,board-profile-id; then
    if test "${x200_loaded_profile}" != "${board_profile_id}"; then
        echo "X200-BOOT: DTB profile mismatch: ${x200_loaded_profile}"
        exit
    fi
else
    echo "X200-BOOT: DTB profile is missing"
    exit
fi

# Select the qualified EPC implementation only after the NOR DT profile gate.
if fdt resize 0x1000; then
    echo "X200-BOOT: DTB capacity=PASS"
else
    echo "X200-BOOT: DTB resize failed"
    exit
fi
if fdt set /soc/pcie-ep@3800000 compatible rhinelab,x200-pcie-ep-diagnostic; then
    echo "X200-BOOT: PCIe publication=Linux runtime"
else
    echo "X200-BOOT: PCIe endpoint DT selection failed"
    exit
fi

setenv bootargs "${x200_console_args} root=PARTUUID=${x200_root_partuuid} rw rootwait pci=pcie_bus_perf ${x200_extra_bootargs} x200_pcie_persistent=v1"
echo "X200-BOOT: handoff=Linux"
booti ${kernel_addr_r} - ${fdt_addr_r}
