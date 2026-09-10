"""Producer integrity, ABI rejection, and card publication guards without hardware."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import runtime_contract as contract

HERE = Path(__file__).resolve().parent

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, HERE / relative)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

card = load('card_runtime', 'persistent/card-runtime.py')


class ProducerTests(unittest.TestCase):
    def test_failed_producer_rejected_before_artifact_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'; path.write_text('{"build_validation":"pending"}')
            with self.assertRaisesRegex(ValueError, 'incomplete or failed'):
                contract.producer(path, Path(directory))

    def test_integrity_and_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); artifact = root / 'Image'; artifact.write_bytes(b'image')
            data = {'build_validation': 'PASS', 'artifacts': [{'path':'Image','sha256':'0'*64,'bytes':5}]}
            path = root / 'manifest.json'; path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
                contract.producer(path, root)
            data['artifacts'][0]['path'] = '../Image'; path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'escapes'):
                contract.producer(path, root)

    def test_crc_checks_real_imports_and_exact_vermagic(self):
        exports = {'symbol': 123}
        self.assertEqual(contract.validate_abi('release SMP', '0x7b symbol', exports, 'release SMP'), exports)
        for magic, versions in [('other SMP','0x7b symbol'), ('release SMP','0x7c symbol'), ('release SMP','')]:
            with self.subTest(magic=magic, versions=versions), self.assertRaises(ValueError):
                contract.validate_abi(magic, versions, exports, 'release SMP')

    def test_card_kernel_gate_precedes_any_publication(self):
        with patch.object(card, 'verify_bundle'), patch.object(card, 'kernel_gate', side_effect=ValueError('wrong kernel')), \
             patch.object(card, 'cold_start') as publish:
            with self.assertRaisesRegex(ValueError, 'wrong kernel'):
                card.start(Path('/unused'), 'digest')
            publish.assert_not_called()

    def test_partial_module_state_never_republishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'ntb').mkdir()
            modules = {'ntb': ('ntb.ko','digest'), 'ntb_transport': ('ntb_transport.ko','digest')}
            with patch.object(card, 'verify_bundle'), patch.object(card, 'kernel_gate'), patch.object(card, 'SYS_MODULE', root), \
                 patch.object(card, 'MODULES', modules), patch.object(card, 'cold_start') as publish:
                with self.assertRaisesRegex(ValueError, 'partial NTB state'):
                    card.start(root, 'digest')
                publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
