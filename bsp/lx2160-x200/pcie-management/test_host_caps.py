#!/usr/bin/env python3
"""Synthetic config-file tests only; never access live PCI sysfs."""
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import check_host_caps as caps


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.procfs = self.root / 'proc'
        (self.procfs / '01').mkdir(parents=True)
        self.boot = self.root / 'boot_id'
        self.boot.write_text('synthetic-boot')
        for function, product in ((0, 0x80c0), (1, 0x8d91)):
            path = self.root / f'0000:01:00.{function}'
            path.mkdir()
            (path / 'vendor').write_text('0x1957')
            (path / 'device').write_text(hex(product))
            (path / 'resource').write_text('0 0 0\n' * 6)
            data = bytearray(4096)
            struct.pack_into('<HH', data, 0, 0x1957, product)
            struct.pack_into('<I', data, 0x100, (0x180 << 20) | (1 << 16) | 1)
            struct.pack_into('<I', data, 0x180, (1 << 16) | 3)
            (path / 'config').write_bytes(data[:256])
            self.config(function).write_bytes(data)

    def config(self, function=0):
        return self.procfs / '01' / f'00.{function}'

    def sys_config(self, function=0):
        return self.root / f'0000:01:00.{function}' / 'config'

    def change(self, offset, value, fmt='<I', function=0):
        path = self.config(function)
        data = bytearray(path.read_bytes())
        struct.pack_into(fmt, data, offset, value)
        path.write_bytes(data)

    def check(self):
        return caps.check('0000:01:00.0', self.root, self.boot, self.procfs)

    def test_retained_and_bounded_reads(self):
        with patch.object(caps.os, 'pread', wraps=os.pread) as read:
            result = self.check()
        self.assertTrue(result['observed_only'])
        self.assertTrue(result['capability_gate_pass'])
        self.assertEqual([(call.args[1], call.args[2]) for call in read.call_args_list],
                         [(4, 0), (4, 0x100), (4, 0x180)] * 2)
        self.assertEqual(result['functions'][1]['identity']['bdf'], '0000:01:00.1')

    def test_all_unsafe_enabled_or_disabled_on_either_pf(self):
        for function in (0, 1):
            for cap_id, (_, offset, mask) in caps.UNSAFE.items():
                for control in (0, mask):
                    with self.subTest(function=function, cap_id=cap_id, control=control):
                        self.change(0x180, (1 << 16) | cap_id, function=function)
                        self.change(0x180 + offset, control, '<H', function)
                        result = self.check()
                        self.assertFalse(result['capability_gate_pass'])
                        entry = result['functions'][function]['capabilities'][-1]
                        self.assertEqual(entry['enabled'], bool(control))
            self.change(0x180, (1 << 16) | 3, function=function)

    def test_malformed_chains(self):
        for header in ((0x100 << 20) | 3, (0x181 << 20) | 3,
                       (0x80 << 20) | 3, 0xffffffff, 0):
            with self.subTest(header=header):
                self.change(0x180, header)
                result = self.check()
                self.assertFalse(result['capability_gate_pass'])
                self.assertTrue(result['functions'][0]['errors'])

    def test_short_header_and_short_control(self):
        self.config().write_bytes(self.config().read_bytes()[:0x182])
        self.assertFalse(self.check()['capability_gate_pass'])
        self.change(0x100, (1 << 16) | 0x0f)
        self.config().write_bytes(self.config().read_bytes()[:0x107])
        self.assertFalse(self.check()['capability_gate_pass'])

    def test_control_outside_config(self):
        self.change(0x100, (0xffc << 20) | 1)
        self.change(0xffc, (1 << 16) | 0x0f)
        result = self.check()
        self.assertFalse(result['capability_gate_pass'])
        self.assertIn('outside config space', result['functions'][0]['errors'][0])

    def test_both_identities_precede_extended_reads(self):
        (self.sys_config(1).parent / 'vendor').write_text('0x1234')
        with patch.object(caps.os, 'pread') as read:
            with self.assertRaises(ValueError):
                self.check()
            read.assert_not_called()

    def test_config_identity_and_header_rejected(self):
        for offset, value, fmt in ((0, 0x1234, '<H'), (14, 1, '<B')):
            with self.subTest(offset=offset):
                original = self.sys_config(1).read_bytes()
                data = bytearray(original)
                struct.pack_into(fmt, data, offset, value)
                self.sys_config(1).write_bytes(data)
                with self.assertRaises(ValueError):
                    self.check()
                self.sys_config(1).write_bytes(original)

    def test_truncated_sysfs_uses_full_proc_config(self):
        self.assertEqual(self.sys_config().stat().st_size, 256)
        self.assertEqual(self.config().stat().st_size, 4096)
        self.assertTrue(self.check()['capability_gate_pass'])

    def test_proc_identity_mismatch_before_capabilities(self):
        self.change(0, 0x1234, '<H')
        with patch.object(caps.os, 'pread', wraps=os.pread) as read:
            result = self.check()
        self.assertFalse(result['capability_gate_pass'])
        self.assertEqual(result['functions'][0]['capabilities'], [])
        self.assertIn('identity differs', result['functions'][0]['errors'][0])
        self.assertEqual([(call.args[1], call.args[2]) for call in read.call_args_list],
                         [(4, 0), (4, 0), (4, 0x100), (4, 0x180)])

    def test_short_proc_identity_and_missing_proc_fail_closed(self):
        self.config().write_bytes(b'xx')
        self.assertFalse(self.check()['capability_gate_pass'])
        self.config().unlink()
        self.assertFalse(self.check()['capability_gate_pass'])

    def test_x86_proc_domain_paths(self):
        self.assertEqual(caps.proc_config_path('0000:02:00.0', self.procfs),
                         self.procfs / '02/00.0')
        self.assertEqual(caps.proc_config_path('0001:02:00.1', self.procfs),
                         self.procfs / '0001:02/00.1')

    def test_explicit_pf0_required(self):
        for bdf in ('01:00.0', '0000:01:00.1', '0000:01:20.0'):
            with self.assertRaises(ValueError):
                caps.check(bdf, self.root, self.boot, self.procfs)


if __name__ == '__main__':
    unittest.main()
