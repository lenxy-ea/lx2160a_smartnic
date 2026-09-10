#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Producer integrity and module ABI rejection tests; no hardware access."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from verify_abi import imported_versions, verify_imports
spec = importlib.util.spec_from_file_location('os_builder', HERE.parent / 'os/build.py')
os_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(os_builder)


class ProducerTests(unittest.TestCase):
    def test_import_crc_validation_rejects_missing_and_changed_symbols(self):
        imports = imported_versions('0x12345678 module_layout\n0x0000abcd dpaa2_mac_connect\n')
        verify_imports(imports, {'module_layout': 0x12345678, 'dpaa2_mac_connect': 0xabcd})
        for exports in ({'module_layout': 0x12345678}, {'module_layout': 1, 'dpaa2_mac_connect': 0xabcd}):
            with self.assertRaises(ValueError):
                verify_imports(imports, exports)

    def test_consumer_rejects_tampered_artifact_and_escaping_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            old_root = os_builder.ROOT
            try:
                os_builder.ROOT = Path(temporary)
                build = Path(temporary) / 'build'
                build.mkdir()
                image = build / 'Image'
                image.write_bytes(b'current producer artifact')
                item = {'path': 'build/Image', 'sha256': os_builder.sha(image), 'bytes': image.stat().st_size}
                data = {'build_validation': 'PASS', 'artifacts': [item]}
                manifest = build / 'manifest.json'
                manifest.write_text(json.dumps(data))
                os_builder.read_manifest(manifest)
                image.write_bytes(b'modified producer artifact')
                with self.assertRaises(ValueError):
                    os_builder.read_manifest(manifest)
                item['path'] = '../outside'
                manifest.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    os_builder.read_manifest(manifest)
            finally:
                os_builder.ROOT = old_root

    def test_overlay_rejects_extras_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'overlay'
            root.mkdir()
            file = root / 'tool'
            file.write_bytes(b'tool')
            manifest = Path(temporary) / 'overlay.json'
            data = {'artifacts': [{'path': 'tool', 'sha256': os_builder.sha(file), 'bytes': 4}], 'symlinks': []}
            manifest.write_text(json.dumps(data))
            os_builder.verify_overlay(root, manifest)
            extra = root / 'unlisted'
            extra.write_bytes(b'extra')
            with self.assertRaises(ValueError):
                os_builder.verify_overlay(root, manifest)
            extra.unlink()
            extra.symlink_to('../escape')
            data['symlinks'] = [{'path': 'unlisted', 'target': '../escape'}]
            manifest.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                os_builder.verify_overlay(root, manifest)

    def test_kernel_consumer_rejects_unlisted_image_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            old_root = os_builder.ROOT
            try:
                os_builder.ROOT = Path(temporary)
                build = Path(temporary) / 'build'
                build.mkdir()
                image = build / 'Image'
                image.write_bytes(b'image')
                item = {'path': 'build/Image', 'sha256': os_builder.sha(image), 'bytes': 5}
                data = {'build_validation': 'PASS', 'artifacts': [item],
                        'image': 'build/Image', 'config': 'build/Image', 'vmlinux': 'build/Image',
                        'module_symvers': 'build/Image', 'image_sha256': item['sha256'],
                        'image_vmlinux_validation': 'PASS'}
                manifest = build / 'manifest.json'
                manifest.write_text(json.dumps(data))
                os_builder.read_kernel_manifest(manifest)
                data['image'] = 'build/other-Image'
                manifest.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    os_builder.read_kernel_manifest(manifest)
            finally:
                os_builder.ROOT = old_root

    def test_installed_module_set_rejects_stale_or_extra_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            old_root = os_builder.ROOT
            try:
                os_builder.ROOT = Path(temporary)
                directory = Path(temporary) / 'build/modules'
                directory.mkdir(parents=True)
                module = directory / 'test.ko'
                module.write_bytes(b'compiled module')
                item = {'path': 'build/modules/test.ko', 'sha256': os_builder.sha(module), 'bytes': 15}
                producer = {'modules_dir': 'build/modules', 'artifacts': [item], 'modules': {'test': item}}
                os_builder.verify_installed_modules(producer)
                module.write_bytes(b'different module')
                with self.assertRaises(ValueError):
                    os_builder.verify_installed_modules(producer)
                module.write_bytes(b'compiled module')
                (directory / 'unlisted.ko').write_bytes(b'extra')
                with self.assertRaises(ValueError):
                    os_builder.verify_installed_modules(producer)
            finally:
                os_builder.ROOT = old_root


if __name__ == '__main__':
    unittest.main()
