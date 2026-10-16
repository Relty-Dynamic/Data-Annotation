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

    def test_system_metadata_is_allowed_but_missing_and_changed_payloads_still_fail(self):
        kit=self.kit()
        for name in ('.DS_Store','wheels/Thumbs.db','tools/desktop.ini'):(kit/name).write_bytes(b'explicit OS metadata fixture')
        self.assertEqual(bootstrap.offline_kit(kit),kit.resolve())
        wheel=kit/'wheels/fixture.whl';wheel.unlink()
        with self.assertRaises(RuntimeError):bootstrap.offline_kit(kit)
        wheel.write_bytes(b'corrupt wheel fixture')
        with self.assertRaises(RuntimeError):bootstrap.offline_kit(kit)

    def test_metadata_aliases_and_lookalike_filenames_are_rejected(self):
        kit=self.kit()
        (kit/'.DS_Store.py').write_text('unlisted code fixture')
        with self.assertRaises(RuntimeError):bootstrap.offline_kit(kit)
        (kit/'.DS_Store.py').unlink()
        (kit/'.DS_Store').symlink_to(kit/'source.zip')
        with self.assertRaises(RuntimeError):bootstrap.offline_kit(kit)

    def test_publisher_listed_metadata_keeps_its_digest_check(self):
        kit=self.kit();metadata=kit/'.DS_Store';metadata.write_bytes(b'listed metadata fixture')
        ledger=json.loads((kit/'files.json').read_bytes())
        ledger['files']['.DS_Store']=hashlib.sha256(metadata.read_bytes()).hexdigest()
        (kit/'files.json').write_text(json.dumps(ledger))
        self.assertEqual(bootstrap.offline_kit(kit),kit.resolve())
        metadata.write_bytes(b'changed metadata fixture')
        with self.assertRaises(RuntimeError):bootstrap.offline_kit(kit)

    def test_download_digest_mismatch_does_not_publish_or_reuse_corrupt_cache(self):
        cache=self.root/'downloads';cache.mkdir()
        checksum=hashlib.sha256(b'expected').hexdigest();(cache/checksum).write_bytes(b'corrupt')
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'wrong')):
            with self.assertRaises(RuntimeError):cached_download('https://official.invalid',checksum,cache)
        self.assertFalse((cache/(checksum+'.part')).exists())

    def test_partial_dependency_download_resumes_from_the_exact_range(self):
        cache=self.root/'resume';cache.mkdir();data=b'abcdefgh';checksum=hashlib.sha256(data).hexdigest()
        (cache/(checksum+'.part')).write_bytes(data[:4])
        response=io.BytesIO(data[4:]);response.status=206;response.headers={'Content-Range':'bytes 4-7/8'}
        def fetch(request,**options):
            self.assertEqual(request.get_header('Range'),'bytes=4-')
            self.assertEqual(options['timeout'],30)
            return response
        with patch('urllib.request.urlopen',side_effect=fetch):
            result=cached_download('https://official.invalid/runtime.tar.gz',checksum,cache)
        self.assertEqual(result.read_bytes(),data)
        self.assertFalse((cache/(checksum+'.part')).exists())

    def test_ignored_range_restarts_instead_of_appending(self):
        cache=self.root/'ignored-range';cache.mkdir();data=b'abcdefgh';checksum=hashlib.sha256(data).hexdigest()
        (cache/(checksum+'.part')).write_bytes(data[:4])
        response=io.BytesIO(data);response.status=200;response.headers={'Content-Length':str(len(data))}
        with patch('urllib.request.urlopen',return_value=response):
            self.assertEqual(cached_download('https://official.invalid/runtime.tar.gz',checksum,cache).read_bytes(),data)

    def test_interrupted_read_retains_partial_bytes_and_retries_with_progress(self):
        import itertools
        data=b'abcdefgh';checksum=hashlib.sha256(data).hexdigest();cache=self.root/'interrupted'
        class Interrupted(io.BytesIO):
            status=200
            headers={'Content-Length':str(len(data))}
            def read(self,size=-1):
                if self.tell():raise OSError('isolated connection loss')
                return super().read(4)
        resumed=io.BytesIO(data[4:]);resumed.status=206;resumed.headers={'Content-Range':'bytes 4-7/8'}
        requests=[]
        def fetch(request,**options):
            requests.append(request)
            return Interrupted(data) if len(requests)==1 else resumed
        tick=itertools.count(0,6)
        with patch('urllib.request.urlopen',side_effect=fetch),patch('backend.upload_dist.time.monotonic',side_effect=lambda:next(tick)),patch('builtins.print') as logs:
            self.assertEqual(cached_download('https://official.invalid/runtime.tar.gz',checksum,cache).read_bytes(),data)
        self.assertEqual(requests[1].get_header('Range'),'bytes=4-')
        self.assertTrue(any('KiB/s' in call.args[0] for call in logs.call_args_list))
        with patch('urllib.request.urlopen',side_effect=AssertionError('verified cache must not download')):
            self.assertEqual(cached_download('https://official.invalid/runtime.tar.gz',checksum,cache).read_bytes(),data)

    def test_unsatisfiable_partial_is_reset_and_verified_again(self):
        import urllib.error
        cache=self.root/'range-416';cache.mkdir();data=b'abcdefgh';checksum=hashlib.sha256(data).hexdigest()
        (cache/(checksum+'.part')).write_bytes(b'oversized stale partial')
        requests=[]
        def fetch(request,**options):
            requests.append(request)
            if len(requests)==1:raise urllib.error.HTTPError(request.full_url,416,'fixture invalid range',{},None)
            self.assertIsNone(request.get_header('Range'))
            response=io.BytesIO(data);response.status=200;response.headers={'Content-Length':'8'}
            return response
        with patch('urllib.request.urlopen',side_effect=fetch):
            self.assertEqual(cached_download('https://official.invalid/runtime.tar.gz',checksum,cache).read_bytes(),data)
        self.assertEqual(len(requests),2)

    def test_invalid_range_cannot_publish_and_keeps_partial_for_retry(self):
        cache=self.root/'invalid-range';cache.mkdir();data=b'abcdefgh';checksum=hashlib.sha256(data).hexdigest()
        partial=cache/(checksum+'.part');partial.write_bytes(data[:4])
        def fetch(*args,**kwargs):
            response=io.BytesIO(data[4:]);response.status=206;response.headers={'Content-Range':'bytes 3-6/8'}
            return response
        with patch('urllib.request.urlopen',side_effect=fetch),self.assertRaises(OSError):
            cached_download('https://official.invalid/runtime.tar.gz',checksum,cache)
        self.assertFalse((cache/checksum).exists());self.assertEqual(partial.read_bytes(),data[:4])

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
