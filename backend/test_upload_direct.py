"""Real loopback requests must work even with unusable inherited proxies."""
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.upload_agent import main
from backend.upload_bootstrap import download
from backend.upload_transfer import transfer_files


class DirectUploadTests(unittest.TestCase):
    def setUp(self):
        self.received=[]
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def dispatch(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                outer.received.append((self.command,self.path,body))
                if self.path=='/api/health':
                    result={'capabilities':['s3-helper-bridge']}
                elif self.path=='/api/r2/helper/register':
                    result={'id':'a'*32,'claim':'local-test-claim','token':'local-test-token'}
                else:result={'ok':True}
                raw=json.dumps(result).encode()
                self.send_response(401 if self.path.endswith('/poll') else 200)
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers();self.wfile.write(raw)
            do_GET=do_POST=do_PUT=dispatch
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.origin=f'http://127.0.0.1:{self.server.server_port}'
        env={name:'http://127.0.0.1:1' for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')}
        env.update(NO_PROXY='',no_proxy='')
        proxy=patch.dict(os.environ,env);proxy.start();self.addCleanup(proxy.stop)

    def test_agent_control_requests_ignore_inherited_proxy(self):
        engine=SimpleNamespace(capabilities=lambda:None,close=lambda:None,snapshot=lambda:{'busy':False},
                               received=None,completed=None,target=SimpleNamespace())
        with patch('sys.argv',['upload_agent','--api',self.origin,'--frontend','https://app.example.com','--browser','chrome']),patch('backend.upload_agent.public_origin'),patch('backend.upload_agent.AgentEngine',return_value=engine),patch('backend.upload_agent.open_handoff',return_value=True),patch('sys.platform','darwin'),patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError,'关联已失效'):
                main()
        self.assertEqual([p for _,p,_ in self.received],['/api/health','/api/r2/helper/register','/api/r2/helper/poll'])

    def test_signed_file_put_ignores_inherited_proxy(self):
        with tempfile.TemporaryDirectory() as raw:
            prepared=Path(raw);(prepared/'video.mp4').write_bytes(b'isolated-upload-fixture')
            def request(method,path,**kwargs):
                return dict(complete=False,mode='single',url=self.origin+'/object',headers={})
            target=SimpleNamespace(attachments=[],request=request,progress=lambda text:None,transfer_progress=lambda values:None)
            transfer_files(target,{'id':'fixture'},prepared,dict(files=[{'key':'video.mp4','size':23}],cache_files=[]))
        self.assertEqual(self.received,[('PUT','/object',b'isolated-upload-fixture')])

    def test_bootstrap_download_ignores_inherited_proxy(self):
        self.assertEqual(json.loads(download(self.origin+'/source')),{'ok':True})
