from concurrent.futures import ThreadPoolExecutor
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader('banner', str(HERE / 'x200-boot-banner'))
spec = importlib.util.spec_from_loader(loader.name, loader)
banner = importlib.util.module_from_spec(spec)
loader.exec_module(banner)


class BannerTests(unittest.TestCase):
    def test_token_encoding_and_bounds(self):
        value = 'a b\n%é=\\'
        self.assertIn(r'a b\x0A%\xC3\xA9=\x5C', banner.line('Kernel', value))
        result = banner.line('Kernel', 'a' * 200)
        self.assertIn('WARN: Kernel: unknown (value too long)', result)
        self.assertLessEqual(len(result), 160)

    def test_os_release_is_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'os-release'
            trap = Path(temp) / 'executed'
            path.write_text(f'ID="$(touch {trap})"\nVERSION_ID="13%a"\n')
            value, reason = banner.os_identity(path)
            self.assertIsNone(reason)
            self.assertIn('$(touch ', banner.line('OS', value))
            self.assertFalse(trap.exists())
            for data in ('ID="unclosed\nVERSION_ID=13\n', 'ID=a\nID=b\nVERSION_ID=13\n',
                         'ID=debian\nVERSION_ID=a b\n', 'not an assignment\n'):
                path.write_text(data)
                self.assertEqual(banner.os_identity(path), (None, 'os_release_malformed'))
            path.write_text('ID=debian\n')
            self.assertEqual(banner.os_identity(path), (None, 'os_release_missing_field'))
            path.unlink()
            self.assertEqual(banner.os_identity(path), (None, 'os_release_unavailable'))

    def test_four_lines_and_restart_dedup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            os_release, boot = root / 'os-release', root / 'boot_id'
            os_release.write_text('ID=debian\nVERSION_ID="13"\n')
            boot.write_text('00000000-0000-4000-8000-000000000001\n')
            output = io.StringIO()
            descriptor = '#1 SMP PREEMPT Wed Jan 01 00:00:00 UTC 2025'
            with patch.object(banner.os, 'uname', return_value=SimpleNamespace(
                    release='6.12.49', version=descriptor)) as uname:
                banner.emit(root / 'run', os_release, boot, output)
                uname.assert_called_once_with()
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 4)
            self.assertEqual(lines[1], '[RhineLab X200] Linux: Build: ' + descriptor)
            self.assertIn('OS: debian 13', lines[2])
            self.assertIn('Boot ID: 00000000-0000-4000-8000-000000000001', lines[3])
            self.assertTrue(all(len(line) <= 160 and line.isascii() for line in lines))
            output = io.StringIO()
            banner.emit(root / 'run', os_release, boot, output)
            self.assertEqual(output.getvalue(), '[RhineLab X200] Linux: banner already shown for this boot\n')

    def test_unavailable_running_kernel_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = io.StringIO()
            with patch.object(banner.os, 'uname', side_effect=OSError('unavailable')):
                banner.emit(root / 'run', root / 'missing', root / 'missing', output)
            self.assertIn('WARN: Kernel: unknown (metadata unavailable)', output.getvalue())
            self.assertIn('WARN: Build: unknown (metadata unavailable)', output.getvalue())
            self.assertEqual(len(output.getvalue().splitlines()), 4)

    def test_missing_identity_dedup_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = io.StringIO()
            banner.emit(root / 'run', root / 'missing', root / 'missing', output)
            self.assertIn('Boot ID: unknown (boot id unavailable)', output.getvalue())
            output = io.StringIO()
            banner.emit(root / 'run', root / 'missing', root / 'missing', output)
            self.assertNotIn('Linux: Kernel:', output.getvalue())
            marker = root / 'run/emitted'
            marker.unlink()
            marker.symlink_to(root / 'victim')
            with self.assertRaises(OSError):
                banner.emit(root / 'run', root / 'missing', root / 'missing', output)
            self.assertFalse((root / 'victim').exists())

    def test_concurrent_invocations_emit_one_enter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def invoke(_):
                output = io.StringIO()
                banner.emit(root / 'run', root / 'missing', root / 'missing', output)
                return output.getvalue()
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(invoke, range(8)))
            self.assertEqual(sum('Linux: Kernel:' in item for item in results), 1)
            self.assertEqual(sum('banner already shown for this boot' in item for item in results), 7)

    def test_offline_install_remove(self):
        with tempfile.TemporaryDirectory() as temp:
            args = ['python3', str(HERE / 'install.py'), '--root', temp]
            subprocess.run(args, check=True)
            subprocess.run(args, check=True)
            self.assertTrue((Path(temp) / 'usr/local/sbin/x200-boot-banner').is_file())
            subprocess.run(args + ['--remove'], check=True)
            self.assertFalse((Path(temp) / 'usr/local/sbin/x200-boot-banner').exists())
        result = subprocess.run(['python3', str(HERE / 'install.py'), '--root', '/'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
