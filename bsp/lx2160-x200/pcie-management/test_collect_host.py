#!/usr/bin/env python3
"""Offline tests of bounded host PCI metadata collection."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('collect_host', Path(__file__).with_name('collect_host.py'))
COLLECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECT)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bdf = '0000:02:00.0'
        self.device = self.root / self.bdf
        self.device.mkdir()
        (self.device / 'vendor').write_text('0x1957')
        (self.device / 'device').write_text('0xe200')
        self.header = bytearray(64)
        struct.pack_into('<HH', self.header, 0, 0x1957, 0xe200)
        (self.device / 'config').write_bytes(self.header)
        (self.device / 'resource').write_text('0 0 0\n' * 6)
        self.boot = self.root / 'boot_id'
        self.boot.write_text('test-boot')

    def collect(self):
        return COLLECT.collect(self.bdf, self.root, self.boot)

    def test_bootstrap_identity_is_not_a_runtime_endpoint(self):
        (self.device / 'device').write_text('0x80c0')
        with self.assertRaisesRegex(ValueError, 'expected X200'):
            self.collect()

    def test_wrong_device_rejected(self):
        (self.device / 'device').write_text('0x1234')
        with self.assertRaisesRegex(ValueError, 'expected X200'):
            self.collect()

    def test_truncated_header_rejected(self):
        (self.device / 'config').write_bytes(self.header[:63])
        with self.assertRaisesRegex(ValueError, 'incomplete PCI config'):
            self.collect()

    def test_identity_mismatch_rejected(self):
        self.header[0] = 0
        (self.device / 'config').write_bytes(self.header)
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            self.collect()

    def test_unassigned_resources_do_not_imply_one_byte(self):
        result = self.collect()
        self.assertEqual(result['bars'][0]['size_bytes'], 0)
        self.assertFalse(result['bars'][0]['assigned'])
        self.assertFalse(result['activation_allowed'])
        self.assertTrue(result['observed_only'])

    def test_64bit_resource_and_flags_and_read_bound(self):
        struct.pack_into('<H', self.header, 4, 7)
        struct.pack_into('<II', self.header, 16, 0x0000000c, 0x12)
        (self.device / 'config').write_bytes(self.header + b'ignored extended config')
        (self.device / 'resource').write_text('1200000000 1200001fff 14220c\n' + '0 0 0\n' * 5)
        result = self.collect()
        self.assertEqual(len(bytes.fromhex(result['config_header_hex'])), 64)
        self.assertTrue(result['memory_space_enabled'])
        self.assertTrue(result['bus_master_enabled'])
        self.assertTrue(result['io_space_enabled'])
        bar = result['bars'][0]
        self.assertEqual(bar['size_bytes'], 8192)
        self.assertTrue(bar['memory_64bit'])
        self.assertTrue(bar['prefetchable'])
        self.assertTrue(result['bars'][1]['upper_half_of_64bit_bar'])
        self.assertFalse(result['activation_allowed'])

    def test_driver_owned_and_sriov_remain_observations(self):
        (self.device / 'driver').symlink_to(self.root / 'drivers' / 'vfio-pci')
        (self.device / 'iommu_group').symlink_to(self.root / 'groups' / '42')
        (self.device / 'sriov_numvfs').write_text('2')
        result = self.collect()
        self.assertEqual(result['driver'], 'vfio-pci')
        self.assertEqual(result['iommu_group'], '42')
        self.assertTrue(result['sriov_enabled'])
        self.assertFalse(result['activation_allowed'])
        self.assertTrue(any('owned' in reason for reason in result['activation_denial_reasons']))

    def test_function_identity_must_match(self):
        (self.device / 'device').write_text('0x8d91')
        with self.assertRaisesRegex(ValueError, 'expected X200'):
            self.collect()

    def test_pf1_identity_accepted(self):
        self.bdf = '0000:02:00.1'
        replacement = self.root / self.bdf
        self.device.rename(replacement)
        self.device = replacement
        (self.device / 'device').write_text('0x8d91')
        struct.pack_into('<H', self.header, 2, 0x8d91)
        (self.device / 'config').write_bytes(self.header)
        self.assertEqual(self.collect()['device'], '8d91')

    def test_short_bdf_rejected(self):
        with self.assertRaisesRegex(ValueError, 'explicit'):
            COLLECT.collect('02:00.0', self.root, self.boot)


if __name__ == '__main__':
    unittest.main()
