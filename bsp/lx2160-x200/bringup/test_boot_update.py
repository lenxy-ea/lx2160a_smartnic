"""Protected spans and write ordering for the current-snapshot NOR transaction."""
import unittest
import json
import struct
from unittest.mock import patch
from pathlib import Path
import tempfile
import boot_update as transaction
from package_boot_update import preserve_environment, package


class BootUpdateTests(unittest.TestCase):
    def test_environment_preserved_metadata_written_last(self):
        old=bytes([0xff])*transaction.SIZE; new=bytearray(old)
        new[0]=0x12;new[0xd00000]=0x34;new[0x9c0000]=0x56;new[0x500000]=0
        merged,sectors=preserve_environment(old,bytes(new))
        self.assertEqual(merged[0x500000:0x520000],old[0x500000:0x520000])
        self.assertEqual(sectors,[0,0xd00000,0x9c0000])

    def test_reserved_and_security_changes_rejected(self):
        old=bytes([0xff])*transaction.SIZE
        for offset in (0x520000,0x600000,0x800000,0x880000,0x900000,0x9e0000,0xa00000):
            new=bytearray(old);new[0]=0;new[offset]=0
            with self.subTest(offset=hex(offset)), self.assertRaisesRegex(RuntimeError,'protected regions'):
                preserve_environment(old,bytes(new))

    def test_package_consumes_current_snapshots_and_fresh_producer(self):
        def image(value, env):
            data=bytearray([0xff])*transaction.SIZE
            data[0x100000]=value;data[0x500000]=env
            metadata={'board_profile_id':'x200-s1_12-s2_05-s3_02-v1',
                      'build':{'git_commit':'a'*40}, 'image_target':{'physical_designator':'D11'},
                      'components':{'fip':{'offset':'0x100000','size':1,'sha256':transaction.sha(bytes([value]))}},
                      'containers':{}}
            payload=json.dumps(metadata).encode()
            slot=struct.pack('<8sII',b'X200FW1\0',1,len(payload))+payload+bytes.fromhex(transaction.sha(payload))
            for offset in (0x9c0000,0x9d0000):data[offset:offset+len(slot)]=slot
            return bytes(data)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);old=image(1,0x77);new=image(2,0xff)
            for name,data in [('old.bin',old),('new.bin',new),('old.scr',b'old boot'),('new.scr',b'new boot')]:
                (root/name).write_bytes(data)
            producer={'validation':'PASS','operation':'pack-independent-bank','image':{'sha256':transaction.sha(new),'size':len(new)}}
            (root/'producer.json').write_text(json.dumps(producer))
            (root/'config.json').write_text(json.dumps({'boot_update':{'bank_label':'D11','mtd_device':'/dev/mtd0'}}))
            result=package(root/'old.bin',root/'new.bin',root/'old.scr',root/'new.scr',root/'producer.json',root/'config.json',root/'package')
            self.assertTrue(result['protected_regions_preserved'])
            self.assertEqual((root/'package/firmware.bin').read_bytes()[0x500000],0x77)
            self.assertEqual(transaction.sha((root/'package/SHA256SUMS').read_bytes()),result['manifest_sha256'])

    def test_unapproved_sector_rejected_before_erase(self):
        with patch.object(transaction.subprocess,'run') as erase:
            with self.assertRaisesRegex(RuntimeError,'outside approved'):
                transaction.write_sector(0,0x500000,bytes(transaction.ERASE))
            erase.assert_not_called()

    def test_failed_erase_readback_prevents_any_write(self):
        with patch.object(transaction.subprocess,'run'), patch.object(transaction.os,'pread',return_value=bytes(transaction.ERASE)), \
             patch.object(transaction.os,'pwrite') as write:
            with self.assertRaisesRegex(RuntimeError,'erase readback'):
                transaction.write_sector(0,0,bytes(transaction.ERASE))
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
