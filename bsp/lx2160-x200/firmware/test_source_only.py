"""Validate an isolated source checkout and reject drift in required inputs."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

BOARD = Path(__file__).resolve().parent.parent


class SourceOnly(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.board = self.root / 'bsp/lx2160-x200'
        for directory in ('firmware', 'flexbuild', 'profiles'):
            shutil.copytree(BOARD / directory, self.board / directory,
                            ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(BOARD / 'hardware-evidence-v1.json', self.board / 'hardware-evidence-v1.json')

    def check(self):
        return subprocess.run([sys.executable, self.board / 'firmware/check_current.py'],
                              cwd=self.root, capture_output=True, text=True)

    def test_isolated_source_contract(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reject_mc_checksum_drift(self):
        path = self.board / 'firmware/inputs-v1.json'
        lock = json.loads(path.read_text())
        next(item for item in lock['artifacts'] if item['name'] == 'mc')['sha256'] = '0' * 64
        path.write_text(json.dumps(lock))
        self.assertNotEqual(self.check().returncode, 0)

    def test_reject_missing_hardware_binding(self):
        current = json.loads((self.board / 'firmware/current.json').read_text())
        path = self.root / current['profile'] / 'profile.json'
        profile = json.loads(path.read_text())
        profile['evidence_bindings']['ddr'] = ['nonexistent']
        path.write_text(json.dumps(profile))
        self.assertNotEqual(self.check().returncode, 0)

    def test_reject_reference_board_authority(self):
        path = self.board / 'hardware-evidence-v1.json'
        registry = json.loads(path.read_text())
        registry['facts']['x200-bl2-ddr-contract']['authority'] = 'lx2160ardb-board-source'
        path.write_text(json.dumps(registry))
        self.assertNotEqual(self.check().returncode, 0)

    def test_reject_unpinned_toolchain(self):
        path = self.board / 'firmware/Containerfile'
        path.write_text('FROM debian:trixie\n')
        self.assertNotEqual(self.check().returncode, 0)


if __name__ == '__main__':
    unittest.main()
