"""Same-page uploader: scoped delegation, retries, expiry and installer safety."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from backend.upload_bridge import UploadBridge, install
from backend.upload_agent import AgentEngine, AgentTarget
from backend.upload_bootstrap import unpack_source
from backend.upload_install import bundle, shell, powershell
from backend.test_r2_integration import USER, OTHER
from backend.test_r2_integration import MemoryObjects


class Accounts:
    def __init__(self):
        self.user=dict(USER)
        self.active=True
        self.tokens=[]

    def request(self,method,path,**kwargs):
        assert (method,path)==('POST','/v1/session')
        self.tokens.append(kwargs['json']['token'])
        if not self.active:raise HTTPException(401,'请先登录。')
        return {'user':self.user,'csrf_hash':'never-sent-to-component'}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.time=100
        self.accounts=Accounts()
        self.bridge=UploadBridge(self.accounts,lambda:self.time)
        self.registration=self.bridge.register({'platform':'darwin','protocol':1})
        self.request=SimpleNamespace(headers={'authorization':'Bearer '+self.registration['token'],'x-upload-agent':self.registration['id']})

    def pair(self):
        return self.bridge.pair(USER,{key:self.registration[key] for key in ('id','claim')},'parent-session-fixture')

    def test_one_use_claim_cross_account_and_credential_redaction(self):
        self.pair()
        value=self.bridge.agent(self.request)
        self.assertEqual(self.bridge.actor(value)['id'],USER['id'])
        report=json.dumps(self.bridge.public(value))
        for secret in ('parent-session-fixture',self.registration['token'],self.registration['claim']):
            self.assertNotIn(secret,report)
        with self.assertRaises(HTTPException):self.pair()
        with self.assertRaises(HTTPException):self.bridge.owned(OTHER,value['id'])
        with self.assertRaises(HTTPException):self.bridge.agent(SimpleNamespace(headers={'authorization':'Bearer invalid','x-upload-agent':value['id']}))

    def test_expired_or_unprivileged_cannot_pair(self):
        with self.assertRaises(HTTPException):
            self.bridge.pair({**USER,'can_upload':False},self.registration,'session')
        self.time+=301
        with self.assertRaises(HTTPException):self.pair()

    def test_command_redelivery_until_received_and_busy_guard(self):
        self.pair()
        value=self.bridge.agent(self.request)
        queued=self.bridge.command(USER,value['id'],{'action':'select'})
        first=self.bridge.poll(value,{})['command']
        self.assertEqual(first['id'],queued['command']['id'])
        self.assertEqual(self.bridge.poll(value,{})['command'],first)
        self.assertIsNone(self.bridge.poll(value,{'received':first['id'],'report':{'busy':True}})['command'])
        with self.assertRaises(HTTPException):self.bridge.command(USER,value['id'],{'action':'select'})
        self.bridge.poll(value,{'received':first['id'],'completed':first['id'],'report':{'busy':False}})
        self.assertIsNone(self.bridge.public(value)['command'])
        self.bridge.command(USER,value['id'],{'action':'start','fields':{'collector':'lixin'}})

    def test_no_remote_paths_shell_or_expired_parent_session(self):
        self.pair()
        value=self.bridge.agent(self.request)
        for command in [{'action':'execute','fields':{}},{'action':'select','fields':{'path':'/private/data'}},{'action':'start','fields':{'shell':'bad'}}]:
            with self.assertRaises(HTTPException):self.bridge.command(USER,value['id'],command)
        self.accounts.user['can_upload']=False
        with self.assertRaises(HTTPException):self.bridge.actor(value)
        self.accounts.user['can_upload']=True
        self.accounts.active=False
        with self.assertRaises(HTTPException):self.bridge.actor(value)

    def test_offline_agent_not_ready_and_commands_cannot_cross_users(self):
        self.pair()
        self.time+=16
        value=self.bridge.owned(USER,self.registration['id'])
        self.assertFalse(self.bridge.public(value)['online'])
        with self.assertRaises(HTTPException):self.bridge.command(USER,value['id'],{'action':'select'})
        self.bridge.poll(value,{'report':{'busy':False}})
        self.assertTrue(self.bridge.public(value)['online'])


class InstallerTests(unittest.TestCase):
    def test_package_excludes_credentials_cache_and_test_data(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'backend').mkdir()
            (root/'backend/module.py').write_text('pass')
            (root/'backend/.env').write_text('private fixture')
            (root/'backend/test_fixture.py').write_text('private fixture')
            (root/'backend/.cache').mkdir()
            (root/'backend/.cache/private.py').write_text('private fixture')
            (root/'requirements.lock.txt').write_text('httpx[socks]==0.28.1')
            with zipfile.ZipFile(io.BytesIO(bundle(root))) as archive:
                self.assertEqual(set(archive.namelist()),{'backend/module.py','requirements.lock.txt'})

    def test_installer_source_rejects_traversal_and_symlinks(self):
        for name in ['../outside','/absolute','backend/../../outside','backend\\bad.py']:
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w') as archive:archive.writestr(name,b'fixture')
            with tempfile.TemporaryDirectory() as raw,self.assertRaises(RuntimeError):unpack_source(buffer.getvalue(),Path(raw))
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            entry=zipfile.ZipInfo('backend/link');entry.external_attr=0o120777<<16;archive.writestr(entry,b'outside')
        with tempfile.TemporaryDirectory() as raw,self.assertRaises(RuntimeError):unpack_source(buffer.getvalue(),Path(raw))

    def test_project_scoped_installer_does_not_change_host_policy(self):
        for content in [shell('https://api.example.com:8443','https://app.example.com'),powershell('https://api.example.com:8443','https://app.example.com')]:
            self.assertIn('UV_NO_MODIFY_PATH',content)
            self.assertIn('DataMarkUploader',content)
            for forbidden in ['sudo ','Set-ExecutionPolicy','setx ','install -g','ExecutionPolicy Bypass']:
                self.assertNotIn(forbidden,content)


class AgentTests(unittest.TestCase):
    def test_duplicate_command_does_not_reopen_native_picker(self):
        with tempfile.TemporaryDirectory() as raw:
            service=SimpleNamespace(local=Path(raw),sessions=SimpleNamespace(close=lambda:None),previews=SimpleNamespace(close=lambda:None))
            engine=AgentEngine(Path(raw),None,service=service)
            command={'id':'same-command','action':'select','fields':{}}
            with patch('backend.upload_agent.threading.Thread') as thread:
                engine.accept(command);engine.accept(command)
                self.assertEqual(thread.call_count,1)

    def test_delegation_client_cannot_call_account_or_project_apis(self):
        target=AgentTarget(None,None)
        for path in ['/api/users','/api/projects','/api/auth/login','/api/account/track-library']:
            with self.assertRaises(RuntimeError):target.request('GET',path)


class PublicBridgeRouteTests(unittest.TestCase):
    def test_browser_pairing_scoped_upload_and_immediate_permission_revocation(self):
        from backend.auth import AuthStore
        from backend.shared_accounts import AccountClient,create_app as accounts_app
        from backend.public_setup import create_app
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            auth=AuthStore(root/'accounts')
            auth.create_user('admin','Admin','fixture admin password','admin')
            user=auth.create_user('worker','Worker','fixture worker password')
            auth.set_upload_permission(user['id'],True)
            api='https://api.example.com:8443';origin='https://app.example.com'
            env={'DATAMARK_PUBLIC_ORIGIN':origin,'DATAMARK_PUBLIC_API_ORIGIN':api,
                 'DATAMARK_R2_CONFIG':'/fixture/config','DATAMARK_R2_STATE':str(root/'public'),'DATAMARK_TASKS_ENABLED':'0'}
            with patch.dict(os.environ,env),patch('backend.r2_store.R2Store',return_value=MemoryObjects()):
                bridge_client=TestClient(accounts_app(root/'accounts'),base_url='http://accounts:8766')
                with TestClient(create_app(accounts=AccountClient(bridge_client)),base_url=api) as browser:
                    agent=TestClient(browser.app,base_url=api)
                    try:
                        headers={'Origin':api}
                        registered=agent.post('/api/r2/helper/register',headers=headers,json={'platform':'darwin','protocol':1}).json()
                        headers.update(Authorization='Bearer '+registered['token'],**{'X-Upload-Agent':registered['id']})
                        self.assertEqual(agent.get('/api/r2/helper/identity',headers=headers).status_code,401)
                        logged=browser.post('/api/auth/login',headers={'Origin':origin},json={'username':'worker','password':'fixture worker password'})
                        csrf={'Origin':origin,'X-CSRF-Token':logged.json()['csrf']}
                        body={key:registered[key] for key in ('id','claim')}
                        self.assertEqual(browser.post('/api/r2/helpers/pair',headers={'Origin':origin},json=body).status_code,403)
                        pair=browser.post('/api/r2/helpers/pair',headers=csrf,json=body)
                        self.assertEqual(pair.status_code,200,pair.text)
                        self.assertEqual(agent.get('/api/r2/helper/identity',headers=headers).json()['id'],user['id'])
                        self.assertEqual(agent.get('/api/r2/helper/prediction/capabilities',headers=headers).json(), {'enabled':False})
                        self.assertEqual(agent.post('/api/r2/helper/prediction/jobs',headers=headers,json={}).status_code,503)
                        self.assertEqual(agent.get('/api/projects',headers=headers).status_code,401)
                        self.assertEqual(agent.get('/api/users',headers=headers).status_code,401)
                        self.assertEqual(agent.post('/api/r2/helper/transfer/uploads',headers=headers,json={}).status_code,422)
                        self.assertEqual(agent.get('/api/r2/helper/transfer/uploads/missing',headers=headers).status_code,404)
                        auth.set_upload_permission(user['id'],False)
                        self.assertEqual(agent.get('/api/r2/helper/prediction/capabilities',headers=headers).status_code,403)
                        self.assertEqual(agent.get('/api/r2/helper/identity',headers=headers).status_code,403)
                        self.assertEqual(agent.post('/api/r2/helper/transfer/uploads',headers=headers,json={}).status_code,403)
                        self.assertEqual(agent.get('/api/r2/helper/transfer/uploads/missing',headers=headers).status_code,403)
                        auth.set_upload_permission(user['id'],True)
                        auth.logout(logged.cookies.get('datamark_session'))
                        self.assertEqual(agent.get('/api/r2/helper/identity',headers=headers).status_code,401)
                    finally:agent.close()


if __name__=='__main__':unittest.main()
