"""Exercise command boundaries against a PTY with U-Boot Enter-repeat behavior."""
import contextlib
import io
import os
from pathlib import Path
import pty
import re
import select
import tempfile
import threading
import unittest
from unittest.mock import patch

import uboot_command_capture as capture


class FakeUBoot:
    def __enter__(self):
        self.master, self.slave = pty.openpty()
        self.device = Path(os.ttyname(self.slave))
        self.last = b'env save'
        self.replays = 0
        self.commands = []
        self.probes = 0
        self.errors = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def run(self):
        pending = bytearray()
        try:
            while not self.stop.is_set():
                if not select.select([self.master], [], [], .02)[0]:
                    continue
                for byte in os.read(self.master, 4096):
                    if byte == 3:
                        self.probes += 1
                        pending.clear()
                        os.write(self.master, b'^C\r\n=> ')
                    elif byte == 13:
                        command = bytes(pending)
                        pending.clear()
                        if not command:
                            self.replays += 1
                            command = self.last
                        self.commands.append(command)
                        self.last = command
                        os.write(self.master, command + b'\r\n')
                        if command == b'run x200_boot':
                            os.write(self.master, b'lx2160ax login: ')
                        else:
                            token = re.search(rb'echo (X200_CMD_[0-9a-f]+)_BEGIN;', command)
                            if token:
                                nonce = token[1]
                                os.write(self.master, nonce + b'_BEGIN\r\n' + nonce + b'_OK\r\n')
                            os.write(self.master, b'=> ')
                    elif byte == 10:
                        # Simulated Linux console after the explicitly requested boot.
                        pending.clear()
                        os.write(self.master, b'\r\nroot@lx2160ax:~# ')
                    else:
                        pending.append(byte)
        except BaseException as exc:
            self.errors.append(exc)

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(1)
        os.close(self.master)
        os.close(self.slave)
        if self.errors:
            raise self.errors[0]


class PromptProbeTests(unittest.TestCase):
    def test_two_commands_do_not_replay_previous_save(self):
        with FakeUBoot() as target, tempfile.TemporaryDirectory() as directory:
            for index in range(2):
                evidence = Path(directory) / f'{index}.raw'
                argv = ['capture', '--device', str(target.device), '--command', 'env save', '--evidence', str(evidence)]
                with patch.object(capture, 'reject_existing_users'), \
                     patch('sys.argv', argv), contextlib.redirect_stdout(io.StringIO()):
                    capture.main()
                self.assertTrue(evidence.with_suffix('.raw.json').exists())
            self.assertEqual(target.replays, 0)
            self.assertEqual(target.probes, 2)
            self.assertEqual(len(target.commands), 2)
            self.assertTrue(all(b'if env save;' in cmd for cmd in target.commands))



if __name__ == '__main__':
    unittest.main()
