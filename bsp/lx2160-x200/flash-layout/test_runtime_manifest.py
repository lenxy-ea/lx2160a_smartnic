#!/usr/bin/env python3
"""Compile production U-Boot manifest.c against read-only host NOR/SHA adapters.

Fixtures generate their own valid FIP, PBL and all nine component records. No
build artifacts or hardware are required.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / 'flexbuild/board-layer/uboot/board/rhinelab/lx2160x200/manifest.c'
PROFILE = 'x200-s1_12-s2_05-s3_02-v1'
SLOTS = (0x9c0000, 0x9d0000)
SHA = lambda b: hashlib.sha256(b).hexdigest()
BL31 = bytes.fromhex('47d4086d4cfe98469b952950cbbd5a00')
BL33 = bytes.fromhex('d6d0eea7fcead54b97829934f234b6e4')


def encode(manifest):
    payload = json.dumps(manifest, separators=(',', ':'), sort_keys=True).encode('ascii')
    return frame(payload)


def frame(payload):
    data = b'X200FW1\0' + struct.pack('<II', 1, len(payload)) + payload + hashlib.sha256(payload).digest()
    assert len(data) <= 0x10000
    return data + b'\xff' * (0x10000 - len(data))


def fixture():
    image = bytearray(b'\xff' * 0x1000000)
    data = dict(rcw=b'R' * 128, bl2=b'B' * 16001, bl31=b'T' * 9003,
                uboot=b'U' * 33007, ddr_phy=b'D' * 10003,
                mc=b'M' * 16393, dpc=b'C' * 8001, dpl=b'L' * 7001, dtb=b'F' * 6001)
    offsets = dict(rcw=0, bl2=0x9000, bl31=0x100000, uboot=0x100000,
                   ddr_phy=0x800000, mc=0xa00000, dpc=0xe00000, dpl=0xd00000, dtb=0xf00000)
    fip = (struct.pack('<IIQ', 0xaa640001, 0x12345678, 0) +
           struct.pack('<16sQQQ', BL31, 136, len(data['bl31']), 0) +
           struct.pack('<16sQQQ', BL33, 136 + len(data['bl31']), len(data['uboot']), 0) +
           struct.pack('<16sQQQ', bytes(16), 136 + len(data['bl31']) + len(data['uboot']), 0, 0) +
           data['bl31'] + data['uboot'])
    for name, payload in data.items():
        if name not in ('bl31', 'uboot'):
            off = offsets[name] + (8 if name == 'rcw' else 0)
            image[off:off + len(payload)] = payload
    image[0x100000:0x100000 + len(fip)] = fip
    def record(offset, blob):
        return dict(offset=f'0x{offset:08x}', size=len(blob), sha256=SHA(blob))
    components = {name: dict(record(offsets[name], blob), version='fixture-v1') for name, blob in data.items()}
    for name in ('rcw', 'dpc', 'dpl', 'dtb'):
        components[name]['board_profile_id'] = PROFILE
    components['rcw']['scope'] = 'on-media-rcw-128'
    components['bl2']['container'] = 'boot-pbl'
    for name in ('bl31', 'uboot'):
        components[name]['container'] = 'fip'
    components['mc'].update(version='258dd3a' + '0' * 33, api_major=10, api_minor=40)
    contract = json.loads((HERE / 'firmware-manifest-contract-v1.json').read_text())
    m = dict(magic='X200FW1', format_version=1, sequence=1,
             image_target=dict(physical_designator='D11', addressing='independent-bank-local',
                               capacity=0x1000000),
             board=dict(model='RhineLab LX2160A X200 SmartNIC', board_revision='unvalidated',
                        compatible_mask='x200-development-only'),
             board_profile_id=PROFILE, firmware_variant='fixture-native18', components=components,
             containers={'boot-pbl': record(0, image[:0x9000 + len(data['bl2'])]),
                         'fip': record(0x100000, fip)},
             build={field: 'fixture' for field in contract['build_fields']},
             security=dict(secure_boot_policy='development-sb-en-0', signature_algorithm='none', signature=''))
    m['build']['git_commit'] = 'a' * 40
    return image, m


class RuntimeManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='x200-manifest-')
        cls.work = Path(cls.temp.name)
        include = cls.work / 'include'
        for name in ('config.h', 'command.h', 'dm.h', 'malloc.h', 'spi_flash.h',
                     'linux/errno.h', 'linux/string.h', 'u-boot/sha256.h'):
            path = include / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#include_next <linux/errno.h>\n' if name == 'linux/errno.h' else '#include "runtime_manifest_stubs.h"\n')
        (include / 'runtime_manifest_stubs.h').write_bytes((HERE / 'runtime_manifest_stubs.h').read_bytes())
        (include / 'manifest.h').write_bytes((SOURCE.parent / 'manifest.h').read_bytes())
        harness = cls.work / 'harness.c'
        harness.write_text(r'''
#include "runtime_manifest_stubs.h"
#include "manifest.h"
static FILE *nor;
static struct udevice flash;
static unsigned int reads, max_read;
static int probe_error;
int spi_flash_probe_bus_cs(unsigned int bus, unsigned int cs, struct udevice **dev) {
    *dev = &flash; return probe_error;
}
int spi_flash_read_dm(struct udevice *dev, u32 offset, size_t len, void *data) {
    reads++;
    if (len > max_read) max_read = len;
    if (offset > 0x1000000 || len > 0x1000000 - offset) return -EIO;
    if (fseek(nor, offset, SEEK_SET) || fread(data, 1, len, nor) != len) return -EIO;
    return 0;
}
extern int host_command(void);
extern void x200_report_boot_flash_identity(void);
int main(int argc, char **argv) {
    int ret;
    if (argc < 2 || !(nor = fopen(argv[1], "rb"))) return 2;
    if (argc > 3 && !strcmp(argv[3], "probe-failure")) probe_error = -EIO;
    if (argc > 2) {
        const struct x200_flash_summary *summary;
        unsigned int before;
        if (x200_boot_flash_summary()) return 3;
        x200_report_boot_flash_identity();
        before = reads;
        summary = x200_boot_flash_summary();
        if (reads != before) return 4;
        if (summary)
            printf("HOST-SUMMARY: bank=%s profile=%s variant=%s mc_api=%u.%u\n",
                   summary->image, summary->profile, summary->variant, summary->mc_api_major, summary->mc_api_minor);
        else puts("HOST-SUMMARY: unknown");
        ret = argc > 3 && !strcmp(argv[3], "verify") ? host_command() : 0;
    }
    else ret = host_command();
    printf("HOST-READS: count=%u max=%u\n", reads, max_read);
    fclose(nor);
    return ret;
}
''')
        cls.exe = cls.work / 'runtime-manifest'
        subprocess.run(['cc', '-std=c99', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-sign-compare', '-Wno-deprecated-declarations',
                        '-I', str(include), str(SOURCE), str(harness), '-lcrypto', '-o', str(cls.exe)], check=True)
        cls.base, cls.manifest = fixture()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_image(self, a=None, b=None, mutate=None, expect=True, report=False,
                  probe_failure=False, verify_after_report=False):
        image = bytearray(self.base)
        for off, value in zip(SLOTS, (self.manifest if a is None else a, self.manifest if b is None else b)):
            image[off:off + 0x10000] = value if isinstance(value, bytes) else encode(value)
        if mutate:
            mutate(image)
        path = self.work / 'nor.bin'
        path.write_bytes(image)
        args = [str(self.exe), str(path)] + (['report'] if report else [])
        if probe_failure:
            self.assertTrue(report)
            args.append('probe-failure')
        if verify_after_report:
            self.assertTrue(report)
            args.append('verify')
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if expect else 1, result.stdout + result.stderr)
        self.assertNotIn('physical_mapping=', result.stdout)
        self.assertNotIn('UNVALIDATED', result.stdout)
        return result.stdout

    def reject(self, modify):
        m = copy.deepcopy(self.manifest)
        modify(m)
        return self.run_image(m, m, expect=False)

    def test_valid_all_components_and_streaming(self):
        out = self.run_image()
        self.assertIn('composition=PASS components=9 containers=2', out)
        self.assertNotIn('X200-FLASH-ID:', out)
        reads = int(out.split('count=')[1].split()[0])
        self.assertGreater(reads, 35)
        self.assertNotIn('manifest_sha256=', out)

    def test_boot_report_then_verify_prints_identity_once_and_checks_components(self):
        out = self.run_image(report=True, verify_after_report=True)
        self.assertEqual(out.count('X200-FLASH-ID:'), 1)
        self.assertIn('X200-FLASH-ID: D11 manifest verified\n', out)
        self.assertIn('composition=PASS', out)
        for detail in ('manifest_sha256=', 'logical_sf=', 'selected_slot=', 'git_commit='):
            self.assertNotIn(detail, out)
        out = self.run_image(report=True, verify_after_report=True, expect=False,
                             mutate=lambda b: b.__setitem__(0xa00000, 0))
        self.assertEqual(out.count('X200-FLASH-ID:'), 1)
        self.assertIn('D11 manifest verified', out)
        self.assertIn('composition=FAIL', out)

    def test_bank_label_comes_from_valid_manifest(self):
        m = copy.deepcopy(self.manifest)
        m['image_target']['physical_designator'] = 'D12'
        for report in (False, True):
            with self.subTest(report=report):
                out = self.run_image(m, m, report=report)
                if report:
                    self.assertIn('X200-FLASH-ID: D12 manifest verified', out)
                    self.assertIn('HOST-SUMMARY: bank=D12 profile=' + PROFILE, out)
                    self.assertIn('mc_api=10.40', out)
                    self.assertNotIn('258dd3a', out)
                else:
                    self.assertNotIn('X200-FLASH-ID:', out)
                self.assertNotIn('image=D11', out)

    def test_probe_failure_keeps_failure_diagnostic(self):
        out = self.run_image(report=True, probe_failure=True)
        self.assertIn('HOST-SUMMARY: unknown', out)
        self.assertIn('image=UNKNOWN sha256=FAIL probe_err=', out)
        self.assertIn('HOST-READS: count=0 max=0', out)

    def test_highest_sequence_full_uint64_and_tie(self):
        for value in (2**32 + 7, 2**64 - 1):
            with self.subTest(value=value):
                b = copy.deepcopy(self.manifest)
                b['sequence'] = value
                b['image_target']['physical_designator'] = 'D12'
                out = self.run_image(b=b, report=True)
                self.assertIn(f'using slot B (sequence {value})', out)
                self.assertIn('WARN manifests differ', out)
                self.assertIn('HOST-SUMMARY: bank=D12', out)
                a = copy.deepcopy(b)
                a['image_target']['physical_designator'] = 'D11'
                self.assertIn('HOST-SUMMARY: bank=D11', self.run_image(a=a, b=b, report=True))
        b = copy.deepcopy(self.manifest)
        b['firmware_variant'] = 'different'
        out = self.run_image(b=b)
        self.assertIn('slot A', out)
        self.assertIn('WARN manifests differ', out)

    def test_highest_valid_metadata_does_not_fallback_on_bad_components(self):
        b = copy.deepcopy(self.manifest)
        b['sequence'] = 2**63
        b['board_profile_id'] = 'wrong'
        out = self.run_image(b=b)
        self.assertIn('slot A', out)
        self.assertIn('WARN only slot', out)
        b['board_profile_id'] = PROFILE
        b['components']['uboot']['sha256'] = '0' * 64
        out = self.run_image(b=b, expect=False)
        self.assertIn('slot B', out)
        self.assertIn('composition=FAIL', out)

    def test_bounded_parser_and_large_metadata(self):
        m = copy.deepcopy(self.manifest)
        m['build']['provenance'] = 'p' * 50000
        self.assertIn('composition=PASS', self.run_image(m, m))
        m['build']['provenance'] = [0] * 2048
        self.run_image(m, m, expect=False)
        nested = 1
        for _ in range(30):
            nested = [nested]
        m['build']['provenance'] = nested
        self.run_image(m, m, expect=False)

    def test_sequence_invalid(self):
        for value in (2**64, -1, True, '2', 1.5):
            with self.subTest(value=value):
                self.reject(lambda m: m.update(sequence=value))

    def test_every_required_field(self):
        contract = json.loads((HERE / 'firmware-manifest-contract-v1.json').read_text())
        for field in contract['required_top_level_fields']:
            with self.subTest(top=field):
                self.reject(lambda m: m.pop(field))
        for group, fields in [('board', contract['board_fields']), ('build', contract['build_fields']),
                              ('security', contract['security_fields'])]:
            for field in fields:
                with self.subTest(group=group, field=field):
                    self.reject(lambda m: m[group].pop(field))
        for name in contract['required_components']:
            with self.subTest(component=name):
                self.reject(lambda m: m['components'].pop(name))
            for field in contract['component_common_fields']:
                with self.subTest(component=name, field=field):
                    self.reject(lambda m: m['components'][name].pop(field))
        for field in contract['mc_extra_fields']:
            self.reject(lambda m: m['components']['mc'].pop(field))

    def test_scope_board_profile_security(self):
        self.reject(lambda m: m['board'].update(model='LX2160ARDB'))
        self.reject(lambda m: m.update(board_profile_id='wrong-profile'))
        for name in ('rcw', 'dpc', 'dpl', 'dtb'):
            self.reject(lambda m: m['components'][name].update(board_profile_id='wrong-profile'))
        self.reject(lambda m: m['components']['rcw'].update(scope='source-rcw'))
        self.reject(lambda m: m['security'].update(secure_boot_policy='secure'))
        self.reject(lambda m: m['image_target'].update(physical_designator='D13'))

    def test_structural_spoofs_duplicates_malformed(self):
        payload = json.dumps(self.manifest, separators=(',', ':'), sort_keys=True).encode()
        bad = [payload.replace(b'"sequence":1', b'"sequence":1,"sequence":2'),
               payload.replace(b'"sequence":1', b'"sequen\\u0063e":1'),
               payload.replace(b'"sequence":1', b'"sequence":01'),
               payload + b'{}', payload[:-1], payload.replace(b'"sequence":1', b'"sequence":1e')]
        for data in bad:
            with self.subTest(payload=data[-100:]):
                self.run_image(frame(data), frame(data), expect=False)
        self.reject(lambda m: (m.pop('sequence'), m['build'].update(sequence=99)))
        self.reject(lambda m: (m['image_target'].pop('physical_designator'),
                               m['build'].update(spoof='"physical_designator":"D11"')))

    def test_slot_framing_damage_and_degraded(self):
        slot = encode(self.manifest)
        for index in (0, 8, 12, 30, 0xffff):
            with self.subTest(index=index):
                damaged = bytearray(slot)
                damaged[index] ^= 1
                out = self.run_image(bytes(damaged), slot)
                self.assertIn('slot B', out)
                self.assertIn('WARN only slot', out)
                self.run_image(bytes(damaged), bytes(damaged), expect=False)
        for size in (0, 0x10000, 0xffffffff):
            damaged = slot[:12] + struct.pack('<I', size) + slot[16:]
            self.run_image(damaged, damaged, expect=False)

    def test_all_digest_and_range_failures(self):
        for group in ('components', 'containers'):
            for name in self.manifest[group]:
                with self.subTest(group=group, name=name):
                    self.reject(lambda m: m[group][name].update(sha256='0' * 64))
                    self.reject(lambda m: m[group][name].update(sha256='g' * 64))
                    self.reject(lambda m: m[group][name].update(offset='0x10000000000000000'))
                    self.reject(lambda m: m[group][name].update(size=0x1000001))
                    self.reject(lambda m: m[group][name].update(size=0))
        self.reject(lambda m: m['components'].update(extra=copy.deepcopy(m['components']['dtb'])))
        self.reject(lambda m: m['containers'].update(extra=copy.deepcopy(m['containers']['fip'])))
        self.reject(lambda m: m['components']['bl2'].update(container='fip'))
        self.reject(lambda m: m['components']['uboot'].update(offset='0x00101000'))

    def test_fip_leaf_size_digest_and_toc(self):
        self.reject(lambda m: m['components']['uboot'].update(size=33008))
        # Recompute parent hash after mutations so TOC/leaf checks must catch them.
        for relative in (0, 16, 56, 96, 112, 120, 128, 136, 136 + 9003):
            with self.subTest(relative=relative):
                image = bytearray(self.base)
                image[0x100000 + relative] ^= 1
                m = copy.deepcopy(self.manifest)
                m['containers']['fip']['sha256'] = SHA(image[0x100000:0x100000 + m['containers']['fip']['size']])
                self.run_image(m, m, mutate=lambda b: b.__setitem__(slice(0x100000, 0x110000), image[0x100000:0x110000]), expect=False)
        for field, value in ((32, 2**64-1), (40, 2**64-1), (72, 136)):
            image = bytearray(self.base)
            struct.pack_into('<Q', image, 0x100000 + field, value)
            m = copy.deepcopy(self.manifest)
            m['containers']['fip']['sha256'] = SHA(image[0x100000:0x100000 + m['containers']['fip']['size']])
            self.run_image(m, m, mutate=lambda b: b.__setitem__(slice(0x100000, 0x110000), image[0x100000:0x110000]), expect=False)

    def test_metadata_diagnostic_does_not_gate_late_init(self):
        out = self.run_image(b'\xff' * 0x10000, b'\xff' * 0x10000, report=True)
        self.assertIn('redundancy=FAIL', out)
        # Full component verification is deliberately deferred to the command.
        out = self.run_image(mutate=lambda b: b.__setitem__(0xa00000, 0), report=True)
        self.assertIn('D11 manifest verified', out)
        self.assertNotIn('X200-FLASH-VERIFY', out)



if __name__ == '__main__':
    unittest.main()
