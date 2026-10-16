"""Complete installers: verified downloads, local-only dependency install and resumable serving."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import upload_bootstrap as bootstrap
from backend.upload_install import bundle, install, shell, powershell, offline_shell
from backend.upload_dist import TARGETS, kit_key, cached_download, build


class UploaderDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        (self.root/'backend').mkdir()
        (self.root/'backend/main.py').write_text('isolated source fixture')
        (self.root/'backend/upload_bootstrap.py').write_text('explicit bootstrap fixture')
        (self.root/'requirements.lock.txt').write_text('fixture==1.0\n')
        (self.root/'requirements.uploader.lock.txt').write_text('fixture==1.0\n')

    def kit(self):
        kit=self.root/'kit';kit.mkdir()
        (kit/'source.zip').write_bytes(bundle(self.root))
        (kit/'wheels').mkdir();(kit/'wheels/fixture.whl').write_bytes(b'wheel fixture')
        (kit/'tools').mkdir()
        for name in ('ffmpeg','ffprobe'):(kit/'tools'/name).write_bytes(b'tool fixture')
        import platform
        target='macos-arm64' if platform.machine().lower() in {'aarch64','arm64'} else 'macos-x64'
        ledger={p.relative_to(kit).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in kit.rglob('*') if p.is_file()}
        (kit/'files.json').write_text(json.dumps({'target':target,'files':ledger}))
        return kit

    def test_version_updates_are_reported_without_mutating_inflight_commands(self):
        from backend.upload_bridge import UploadBridge
        current=['a'*64]
        bridge=UploadBridge(None,current_version=lambda:current[0])
        registered=bridge.register({'platform':'darwin','protocol':1,'component_version':current[0]})
        value=bridge.agents[registered['id']]
        self.assertFalse(bridge.public(value)['update_available'])
        value['command']={'id':'inflight','action':'start'}
        current[0]='b'*64
        self.assertTrue(bridge.public(value)['update_available'])
        self.assertEqual(bridge.public(value)['command'],{'id':'inflight','action':'start'})
        legacy=bridge.register({'platform':'darwin','protocol':1})
        self.assertTrue(bridge.public(bridge.agents[legacy['id']])['update_available'])

    def test_component_lock_keeps_server_versions_and_excludes_server_only_modules(self):
        root=Path(__file__).resolve().parents[1]
        full=set((root/'requirements.lock.txt').read_text().splitlines())
        component=set((root/'requirements.uploader.lock.txt').read_text().splitlines())
        self.assertTrue(component)
        self.assertTrue(component.issubset(full))
        self.assertFalse(any(line.startswith(('cryptography==','boto3==','botocore==')) for line in component))
        self.assertTrue(any(line.startswith('httpx[socks]==') for line in component))

    def test_source_bundle_is_reproducible_and_changes_with_code(self):
        first=bundle(self.root)
        with patch('time.localtime',return_value=(2030,1,1,0,0,0,1,1,0)):
            self.assertEqual(bundle(self.root),first)
        key=kit_key(self.root)
        (self.root/'backend/main.py').write_text('changed source fixture')
        self.assertNotEqual(kit_key(self.root),key)

    def test_component_version_ignores_zip_platform_metadata(self):
        first=kit_key(self.root)
        payload=bundle(self.root);changed=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(payload)) as source,zipfile.ZipFile(changed,'w') as archive:
            for old in source.infolist():
                item=zipfile.ZipInfo(old.filename,(2025,1,1,0,0,0));item.create_system=0
                archive.writestr(item,source.read(old))
        with patch('backend.upload_install.bundle',return_value=changed.getvalue()):
            self.assertEqual(kit_key(self.root),first)

    def test_offline_install_never_downloads_and_keeps_browser(self):
        kit=self.kit();target=self.root/'install';(target/'.tmp').mkdir(parents=True)
        calls=[]
        def run(args,**options):calls.append(args);return subprocess.CompletedProcess(args,0)
        args=['bootstrap','--root',str(target),'--offline-dir',str(kit),'--browser','chrome']
        with (patch('sys.argv',args),patch.object(bootstrap,'download',side_effect=AssertionError('offline network')),
              patch.object(bootstrap.subprocess,'run',side_effect=run)):
            bootstrap.main()
        pip=next(args for args in calls if 'install' in args)
        self.assertIn('--no-index',pip);self.assertIn('--find-links',pip)
        self.assertFalse(any('https://' in item for item in pip))
        self.assertEqual(calls[-1][-2:],['--browser','chrome'])
        self.assertTrue((target/'app/backend/main.py').is_file())
        self.assertTrue((target/'.tools/ffmpeg/bin/ffmpeg').is_file())
        self.assertFalse((target/'outside').exists())

    def test_corrupt_missing_extra_and_traversal_files_are_rejected(self):
        kit=self.kit()
        self.assertEqual(bootstrap.offline_kit(kit),kit.resolve())
        original=(kit/'source.zip').read_bytes()
        (kit/'source.zip').write_bytes(b'corrupt')
        with self.assertRaises(RuntimeError):bootstrap.offline_kit(kit)
        (kit/'source.zip').write_bytes(original)
        (kit/'extra').write_text('unlisted')
        with self.assertRaises(RuntimeError):bootstrap.offline_kit(kit)
        (kit/'extra').unlink()
        ledger=json.loads((kit/'files.json').read_text());ledger['files']['../outside']='a'*64
        (kit/'files.json').write_text(json.dumps(ledger))
        with self.assertRaises(RuntimeError):bootstrap.offline_kit(kit)

    def test_download_digest_mismatch_does_not_publish_or_reuse_corrupt_cache(self):
        cache=self.root/'downloads';cache.mkdir()
        checksum=hashlib.sha256(b'expected').hexdigest();(cache/checksum).write_bytes(b'corrupt')
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'wrong')):
            with self.assertRaises(RuntimeError):cached_download('https://official.invalid',checksum,cache)
        self.assertFalse((cache/(checksum+'.part')).exists())

    def test_scripts_only_download_from_platform_and_preserve_scoped_install(self):
        kits={target:{'file':target+'-'+'a'*64+'.zip','sha256':'a'*64,'size':123} for target in TARGETS}
        for script in (shell('https://api.example.com','https://app.example.com','chrome',kits),
                       powershell('https://api.example.com','https://app.example.com','chrome',kits)):
            self.assertIn('/api/r2/uploader/packages/',script)
            self.assertIn('--continue-at',script)
            self.assertIn('DataMarkUploader',script)
            for forbidden in ('github.com','astral.sh','pypi.','Set-ExecutionPolicy','sudo ','setx '):self.assertNotIn(forbidden,script)
        script=self.root/'install.sh';script.write_text(shell('https://api.example.com','https://app.example.com',kits=kits))
        subprocess.run(['sh','-n',str(script)],check=True)
        script.write_text(offline_shell());subprocess.run(['sh','-n',str(script)],check=True)

    def test_builder_publishes_complete_kits_and_reuses_verified_existing_output(self):
        runtime=self.root/'runtime.tar.gz';runtime.write_bytes(b'explicit runtime fixture')
        tools=self.root/'tools.zip'
        with zipfile.ZipFile(tools,'w') as archive:
            for name in ('ffmpeg','ffprobe','ffmpeg.exe','ffprobe.exe'):archive.writestr('bin/'+name,b'explicit tool fixture')
            archive.writestr('LICENSE.txt','explicit license fixture')
        def download_wheels(args,**kwargs):
            self.assertEqual(Path(args[args.index('-r')+1]).name,'requirements.uploader.lock.txt')
            destination=Path(args[args.index('--dest')+1]);destination.mkdir(parents=True,exist_ok=True)
            (destination/'fixture-1.0-py3-none-any.whl').write_bytes(b'explicit wheel fixture')
        for target in TARGETS:
            output=self.root/target
            with (patch('backend.upload_dist.python_archive',return_value=runtime),
                  patch('backend.upload_dist.cached_download',return_value=tools),
                  patch('backend.upload_dist.subprocess.run',side_effect=download_wheels)):
                info=build(self.root,output,target,'https://index.invalid/simple')
            path=output/info['file']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),info['sha256'])
            with zipfile.ZipFile(path) as archive:
                ledger=json.loads(archive.read('files.json'))
                self.assertEqual(ledger['target'],target)
                for name,checksum in ledger['files'].items():self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(),checksum)
                self.assertIn('python.tar.gz',ledger['files']);self.assertIn('source.zip',ledger['files'])
                self.assertIn('install.ps1',ledger['files']);self.assertIn('install.command',ledger['files'])
                self.assertTrue(any(name.startswith('wheels/') for name in ledger['files']))
                self.assertTrue(any('LICENSE' in name for name in ledger['files']))
            with patch('backend.upload_dist.python_archive',side_effect=AssertionError('must reuse')):
                self.assertEqual(build(self.root,output,target,'https://index.invalid/simple'),info)

    def test_failed_build_never_marks_a_partial_kit_ready(self):
        runtime=self.root/'runtime.tar.gz';runtime.write_bytes(b'explicit runtime fixture')
        output=self.root/'failed-output'
        with patch('backend.upload_dist.python_archive',return_value=runtime),patch('backend.upload_dist.subprocess.run',side_effect=subprocess.CalledProcessError(1,'pip')):
            with self.assertRaises(subprocess.CalledProcessError):build(self.root,output,'windows-x64','https://index.invalid/simple')
        self.assertFalse((output/'windows-x64.json').exists())
        self.assertFalse(list(output.glob('*.zip')))

    def test_routes_serve_current_verified_kits_with_range_and_refuse_missing_stale_kits(self):
        distribution=self.root/'dist';distribution.mkdir()
        for target in TARGETS:
            data=('explicit installer fixture '+target).encode();checksum=hashlib.sha256(data).hexdigest()
            name=target+'-'+checksum+'.zip';(distribution/name).write_bytes(data)
            (distribution/(target+'.json')).write_text(json.dumps(dict(file=name,size=len(data),sha256=checksum,source_key=kit_key(self.root))))
        environment={'DATAMARK_PUBLIC_API_ORIGIN':'https://api.example.com','DATAMARK_PUBLIC_ORIGIN':'https://app.example.com','DATAMARK_UPLOADER_DIST':str(distribution)}
        def client():
            app=FastAPI()
            with patch.dict(os.environ,environment):install(app,self.root)
            return TestClient(app)
        with client() as api:
            catalog=api.get('/api/r2/uploader/packages').json();self.assertTrue(catalog['ready'])
            self.assertEqual(api.get('/api/r2/uploader/install.sh?browser=chrome').status_code,200)
            self.assertEqual(api.get('/api/r2/uploader/install.ps1').status_code,200)
            item=catalog['platforms']['windows-x64']
            response=api.get('/api/r2/uploader/packages/'+item['file'],headers={'Range':'bytes=2-7'})
            self.assertEqual(response.status_code,206);self.assertEqual(len(response.content),6)
            self.assertIn(item['sha256'],api.get('/api/r2/uploader/packages/'+item['file']+'.sha256').text)
            self.assertEqual(api.get('/api/r2/uploader/packages/not-allowed.zip').status_code,404)
        (self.root/'backend/main.py').write_text('new version')
        with client() as api:
            self.assertFalse(api.get('/api/r2/uploader/packages').json()['ready'])
            self.assertEqual(api.get('/api/r2/uploader/install.sh').status_code,503)
            self.assertEqual(api.get('/api/r2/uploader/install.ps1').status_code,503)


if __name__=='__main__':unittest.main()
