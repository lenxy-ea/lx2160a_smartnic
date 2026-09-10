"""Verify compressed UART payloads fail closed before shell execution."""
import base64
import gzip
import subprocess
import unittest

from uart_run import make_paste


class UartPayloadTest(unittest.TestCase):
    def execute(self, paste):
        return subprocess.run(['bash'], input=paste, capture_output=True)

    def test_payload_and_exit_status(self):
        result = self.execute(make_paste(b"printf '%s\\n' 'literal $() `text`'\nexit 17\n", 'DONE', '# END'))
        self.assertEqual(result.returncode, 17)
        self.assertIn(b'literal $() `text`\n', result.stdout)
        self.assertIn(b'DONE status=17', result.stdout)

    def test_corrupt_gzip_never_executes(self):
        payload = b'echo EXECUTED\n'
        paste = make_paste(payload, 'DONE', '# END')
        compressed = bytearray(gzip.compress(payload, mtime=0))
        compressed[-8] ^= 1  # CRC failure after decompression produced content.
        paste = paste.replace(base64.encodebytes(gzip.compress(payload, mtime=0)),
                              base64.encodebytes(compressed))
        result = self.execute(paste)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'EXECUTED', result.stdout)

    def test_wrong_plaintext_digest_never_executes(self):
        payload = b'echo EXECUTED\n'
        paste = make_paste(payload, 'DONE', '# END')
        paste = paste.replace(base64.encodebytes(gzip.compress(payload, mtime=0)),
                              base64.encodebytes(gzip.compress(payload + b'# changed\n', mtime=0)))
        result = self.execute(paste)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'FAILED', result.stdout)
        self.assertNotIn(b'EXECUTED', result.stdout)


if __name__ == '__main__':
    unittest.main()
