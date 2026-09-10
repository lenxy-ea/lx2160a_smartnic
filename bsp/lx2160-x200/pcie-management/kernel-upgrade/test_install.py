"""Journaled update and rollback tests use a synthetic filesystem only."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('upgrade', Path(__file__).with_name('install.py'))
upgrade = importlib.util.module_from_spec(spec); spec.loader.exec_module(upgrade)


class UpgradeTests(unittest.TestCase):
    def fixture(self, root):
        package, target = root / 'package', root / 'target'
        (package / 'card').mkdir(parents=True)
        (target / 'boot').mkdir(parents=True); (target / 'boot/Image').write_bytes(b'old Image')
        boot = target / 'proc/sys/kernel/random/boot_id'; boot.parent.mkdir(parents=True); boot.write_text('boot-one\n')
        for name, data in {'runtime.json': json.dumps({'image_sha256':upgrade.sha(b'new Image'),'build_id':'new-build'}),
                           'card.service':'[Service]\nExecStart=@BUNDLE@ @SHA256@\n',
                           '90-x200-vntb-unmanaged.conf':'unmanaged\n', 'card-modules.conf':'modules\n'}.items():
            (package / 'card' / name).write_text(data)
        files = sorted((package / 'card').iterdir())
        (package / 'card/SHA256SUMS').write_text(''.join(f'{upgrade.sha(p.read_bytes())}  {p.name}\n' for p in files))
        digest = upgrade.sha((package / 'card/SHA256SUMS').read_bytes())
        (package / 'Image').write_bytes(b'new Image'); (package / 'install.py').write_text('# synthetic\n')
        (package / 'upgrade.json').write_text(json.dumps({'image_sha256':upgrade.sha(b'new Image'),
                'kernel_build_id':'new-build','card_bundle_sha256':digest}))
        files = sorted(p for p in package.rglob('*') if p.is_file())
        (package / 'SHA256SUMS').write_text(''.join(f'{upgrade.sha(p.read_bytes())}  {p.relative_to(package)}\n' for p in files))
        return package, target, upgrade.sha((package / 'SHA256SUMS').read_bytes())

    def test_stage_and_restore_preserves_original_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package, target, expected = self.fixture(root); journal=root/'journal'
            result = upgrade.stage(package, expected, target, journal, upgrade.sha(b'old Image'), 'boot-one')
            self.assertEqual(result['status'],'STAGED'); self.assertEqual((target/'boot/Image').read_bytes(),b'new Image')
            self.assertEqual(upgrade.rollback(target,journal)['status'],'ROLLED_BACK')
            self.assertEqual((target/'boot/Image').read_bytes(),b'old Image')

    def test_wrong_current_image_fails_before_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); package,target,expected=self.fixture(root); journal=root/'journal'
            with self.assertRaisesRegex(ValueError,'current Image changed'):
                upgrade.stage(package,expected,target,journal,'0'*64,'boot-one')
            self.assertFalse(journal.exists())

    def test_foreign_change_prevents_all_rollback_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); package,target,expected=self.fixture(root); journal=root/'journal'
            upgrade.stage(package,expected,target,journal,upgrade.sha(b'old Image'),'boot-one')
            (target/'boot/Image').write_bytes(b'foreign')
            with patch.object(upgrade,'replace',wraps=upgrade.replace) as write:
                with self.assertRaisesRegex(ValueError,'foreign change'):
                    upgrade.rollback(target,journal)
                write.assert_not_called()

    def test_tampered_package_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); package,target,expected=self.fixture(root)
            (package/'Image').write_bytes(b'corrupted')
            with self.assertRaisesRegex(ValueError,'artifact mismatch'):
                upgrade.verify(package,expected)


if __name__ == '__main__':
    unittest.main()
