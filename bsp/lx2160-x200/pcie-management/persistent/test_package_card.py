"""Card bundle assembly and overlay identity from a synthetic current producer."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import package_card


class PackageTests(unittest.TestCase):
    def test_declared_compiled_modules_and_overlay_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); output=root/'kernel-output'; output.mkdir()
            modules={}; records={}
            for name,filename in [('ntb','ntb.ko'),('ntb_transport','ntb_transport.ko'),('ntb_netdev','ntb_netdev.ko'),('pci_epf_vntb','pci-epf-vntb.ko')]:
                path=output/filename;path.write_bytes(name.encode());relative='kernel-output/'+filename
                records[relative]=path;modules[name]={'path':relative,'sha256':package_card.sha(path)}
                duplicate=root/'installed'/filename;duplicate.parent.mkdir(exist_ok=True);duplicate.write_bytes(path.read_bytes())
                records['installed/'+filename]=duplicate
            image=output/'arch/arm64/boot/Image';image.parent.mkdir(parents=True);image.write_bytes(b'kernel')
            records['kernel-output/arch/arm64/boot/Image']=image
            symvers=output/'Module.symvers';symvers.write_text('0x7b symbol provider EXPORT_SYMBOL\n');records['kernel-output/Module.symvers']=symvers
            data={'kernel_release':'test-release','kernel_version':'test-version','build_id':'1'*40,
                  'vermagic':'test-release SMP modversions aarch64','image_sha256':package_card.sha(image),'modules':modules}
            manifest=root/'manifest.json';manifest.write_text(json.dumps(data))
            config=package_card.HERE.parent/'config.example.json'
            def checked(path,expected,exports):
                self.assertEqual(path.parent,output)
                return {'sha256':package_card.sha(path),'vermagic':expected,'crc_validation':'PASS','imported_symbol_count':1}
            with patch.object(package_card,'producer',return_value=(data,records)),patch.object(package_card,'module_check',side_effect=checked):
                result=package_card.package(manifest,root,config,root/'package')
            overlay=package_card.stage_rootfs(root/'package',root/'overlay')
            self.assertEqual(overlay['kernel_build_id'],data['build_id'])
            self.assertEqual(overlay['image_sha256'],data['image_sha256'])
            self.assertEqual(overlay['card_bundle_sha256'],result['card_bundle_sha256'])
            self.assertEqual(len(overlay['symlinks']),1)
            for item in overlay['artifacts']:
                self.assertEqual(package_card.sha(root/'overlay'/item['path']),item['sha256'])


if __name__ == '__main__':
    unittest.main()
