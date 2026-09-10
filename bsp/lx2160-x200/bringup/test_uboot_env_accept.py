import unittest

import uboot_env_accept as accept


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.spec = accept.plan('a' * 32)
        self.captures = {}
        for step in self.spec['steps']:
            name = step['name']
            token = f"X200_ENV_{self.spec['nonce']}_{name}"
            body = ('Environment can be persisted\nx200_pcie_boot_policy=linux-publish-only\n'
                    if name == 'preflight' else
                    f"{accept.VARIABLE}={self.spec['nonce']}_{name[-1]}\n"
                    f"Erasing SPI flash...\nWriting to SPI flash...\ndone\nValid environment: {name[-1]}\n")
            self.captures[name] = f'{token}_BEGIN\n{body}{token}_OK\n=> '.encode()

    def test_two_slots_pass(self):
        self.assertEqual(accept.verify(self.spec, self.captures)['slots'], [1, 2])

    def test_echo_only_missing_prompt_wrong_nonce_reboot_and_same_slot_rejected(self):
        original = self.captures['save2']
        for bad in (
            self.spec['steps'][2]['command'].encode() + b'\n=> ',
            original.removesuffix(b'=> '),
            original.replace(b'a' * 32, b'b' * 32),
            original.replace(b'Writing to SPI flash...', b'NOTICE:  BL2: v1\nWriting to SPI flash...'),
            original.replace(b'Valid environment: 2', b'Valid environment: 1'),
            original.replace(b'_OK\n', b'_FAIL\n'),
            original.replace(b'_2\n', b'_wrong\n'),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                accept.verify(self.spec, {**self.captures, 'save2': bad})

    def test_plan_injection_rejected(self):
        with self.assertRaises(ValueError):
            accept.plan('x; reset')
        altered = {**self.spec, 'variable': 'ethaddr'}
        with self.assertRaises(ValueError):
            accept.verify(altered, self.captures)

    def test_prompt_probe_replaying_prior_save_is_rejected(self):
        replay = b'Saving Environment to SPIFlash... Erasing SPI flash...Writing to SPI flash...done\nValid environment: 2\n=> '
        for data in (replay + b'\n' + self.captures['save2'], self.captures['save2'] + b'\n' + replay):
            with self.assertRaisesRegex(ValueError, 'unexpected environment write'):
                accept.verify(self.spec, {**self.captures, 'save2': data})


if __name__ == '__main__':
    unittest.main()
