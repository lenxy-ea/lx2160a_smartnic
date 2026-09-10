#!/usr/bin/env python3
"""Fixtures independently construct the on-flash format with struct/zlib."""
import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

import inspect_env as target


def slot(payload=b'a=one\0\0', flags=0, corrupt=False):
    data = payload + b'\0' * (65531 - len(payload))
    crc = zlib.crc32(data) ^ (1 if corrupt else 0)
    return struct.pack('<IB', crc, flags) + data


def image(first, second):
    raw = bytearray(b'\xff' * 16777216)
    raw[0x500000:0x510000] = first
    raw[0x510000:0x520000] = second
    return raw


class InspectTests(unittest.TestCase):
    def test_double_blank(self):
        report = target.inspect_image(b'\xff' * 16777216)
        self.assertIsNone(report['selected_slot'])
        self.assertEqual([s['state'] for s in report['slots']], ['blank', 'blank'])

    def test_single_valid_either_position(self):
        for valid in (0, 1):
            slots = [slot(corrupt=True), slot(corrupt=True)]
            slots[valid] = slot()
            self.assertEqual(target.inspect_image(image(*slots))['selected_slot'], valid)

    def test_serial_rules(self):
        for first, second, chosen in ((2, 3, 1), (3, 2, 0), (7, 7, 0),
                                       (255, 0, 1), (0, 255, 0),
                                       (255, 254, 0), (254, 255, 1),
                                       (0, 0, 0), (255, 255, 0)):
            with self.subTest(first=first, second=second):
                report = target.inspect_image(image(slot(flags=first), slot(flags=second)))
                self.assertEqual(report['selected_slot'], chosen)

    def test_both_corrupt(self):
        report = target.inspect_image(image(slot(corrupt=True), slot(corrupt=True)))
        self.assertIsNone(report['selected_slot'])
        self.assertEqual([s['state'] for s in report['slots']], ['corrupt', 'corrupt'])
        self.assertTrue(all(s['syntax_valid'] for s in report['slots']))

    def test_syntax_error_does_not_change_crc_selection(self):
        for payload, expected in ((b'a=one\0a=two\0\0', 'duplicate'),
                                  (b'=bad\0\0', 'empty key'),
                                  (b'bad\0\0', 'missing equals'),
                                  (b'x' * 65531, 'double-NUL')):
            report = target.inspect_image(image(slot(payload, 2), slot(flags=1)), 'a')
            self.assertEqual(report['selected_slot'], 0)
            self.assertTrue(report['slots'][0]['crc_valid'])
            self.assertFalse(report['slots'][0]['syntax_valid'])
            self.assertIn(expected, report['slots'][0]['syntax_error'])
            self.assertEqual(report['get']['status'], 'selected_slot_syntax_invalid')

    def test_empty_environment_and_values_with_equals(self):
        report = target.inspect_image(image(slot(b'\0\0'), slot(corrupt=True)))
        self.assertEqual(report['slots'][0]['key_count'], 0)
        report = target.inspect_image(image(slot(b'a=x=y\0empty=\0\0'), slot(corrupt=True)), 'a')
        self.assertEqual(report['get']['value'], 'x=y')
        self.assertEqual(report['slots'][0]['key_count'], 2)

    def test_default_does_not_reveal_keys_or_values(self):
        raw = image(slot(b'SECRET_KEY=SECRET_VALUE\0\0'), slot(corrupt=True))
        encoded = json.dumps(target.inspect_image(raw))
        self.assertNotIn('SECRET_KEY', encoded)
        self.assertNotIn('SECRET_VALUE', encoded)
        self.assertEqual(target.inspect_image(raw, 'missing')['get']['status'], 'absent')
        self.assertEqual(target.inspect_image(raw, 'SECRET_KEY')['get']['value'], 'SECRET_VALUE')

    def test_blank_means_erased_ff_not_zero(self):
        report = target.inspect_image(image(bytes(65536), bytes(65536)))
        self.assertEqual([s['state'] for s in report['slots']], ['corrupt', 'corrupt'])

    def test_get_unavailable_and_non_utf8(self):
        report = target.inspect_image(b'\xff' * 16777216, 'a')
        self.assertEqual(report['get']['status'], 'no_crc_valid_slot')
        report = target.inspect_image(image(slot(b'a=\xff\0\0'), slot(corrupt=True)), 'a')
        self.assertIsNone(report['get']['value'])
        self.assertEqual(report['get']['value_hex'], 'ff')

    def test_exact_size(self):
        for size in (0, 65536, 16777215, 16777217):
            with self.assertRaises(ValueError):
                target.inspect_image(b'\xff' * size)

    def test_cli_io_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, 'argv', ['inspect_env', str(Path(directory) / 'missing')]):
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(target.main(), 1)
                self.assertIn('error', json.loads(stderr.getvalue()))


if __name__ == '__main__':
    unittest.main()
