#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import unittest

import check_source


class SourceBoundaryTests(unittest.TestCase):
    def test_text_source_allowed(self):
        self.assertEqual(check_source.check_blob("tools/example.py", b"print('ok')\n"), [])

    def test_archives_and_links_rejected(self):
        for path, data, mode in [("build/image.bin", b"\0", "100644"),
                                 ("tools/link", b"../../outside", "120000"),
                                 ("../outside.py", b"pass", "100644")]:
            with self.subTest(path=path):
                self.assertTrue(check_source.check_blob(path, data, mode))

    def test_secret_error_does_not_echo_value(self):
        secret = ("gh" + "p_") + "z" * 36
        errors = check_source.check_blob("config.json", secret.encode())
        self.assertTrue(errors)
        self.assertNotIn(secret, "\n".join(errors))

    def test_archive_reference_in_code_rejected(self):
        text = ("analysis/" + "target-logs/" + "example/receipt.json").encode()
        self.assertTrue(check_source.check_blob("tools/example.py", text))


if __name__ == "__main__":
    unittest.main()
