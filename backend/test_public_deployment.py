"""Exercise publication against private temporary SQLite state and fake Docker."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deploy.ci_deploy_public import (DeploymentError, FILES, PublicDeployment,
                                     prepare_release, replace_image, verify_repository)

OLD, NEW = 'a' * 40, 'b' * 40


class PublicDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'public'
        self.home.mkdir()
        self.previous = self.home / 'releases' / ('r2-' + OLD)
        self.release = self.home / 'releases' / ('r2-' + NEW)
        for release in (self.previous, self.release):
            (release / 'deploy').mkdir(parents=True)
            for name in FILES:
                (release / 'deploy' / name).write_text('test compose fixture')
        (self.release / '.release-commit').write_text(NEW)
        (self.previous / 'deploy/caddy-config').mkdir()
        (self.home / 'current').symlink_to(self.previous)
        self.config = 'DATAMARK_SETUP_IMAGE=datamark-r2:' + OLD + '\nPRESERVED_SETTING=yes\n'
        (self.home / 'runtime.conf').write_text(self.config)
        self.account = Path(self.temp.name).resolve() / 'accounts'
        self.account.mkdir()
        self.db(self.account / 'auth.sqlite3')
        api_volumes = []
        for source, target in [('r2-state', '/r2-state'), ('tasks/state', '/task-state'),
                               ('tasks/artifacts', '/task-artifacts')]:
            path = self.home / source
            path.mkdir(parents=True)
            api_volumes.append(dict(type='bind', source=str(path), target=target))
        self.live = self.home / 'r2-state/projects.sqlite3'
        self.db(self.live)
        self.db(self.home / 'tasks/state/tasks.sqlite3')
        (self.home / 'tasks/artifacts/result.json').write_text('immutable result')
        (self.home / 'secrets').mkdir()
        secret = self.home / 'secrets/r2.json'
        secret.write_text('test-only credential fixture')
        api_volumes.append(dict(type='bind', source=str(secret), target='/run/secrets/r2.json', read_only=True))
        account_volumes = [dict(type='bind', source=str(self.account), target='/accounts/.local')]
        self.rendered = {'services': {
            'gateway': {'ports': [{'published': '8443', 'host_ip': '192.168.2.25'}]},
            'api': {'image': 'datamark-r2:' + NEW, 'volumes': api_volumes, 'environment': {
                'DATAMARK_PUBLIC_API_ORIGIN': 'https://api-annotate.reltydynamic.com:8443',
                'DATAMARK_PUBLIC_ORIGIN': 'https://datamark.reltydynamic.com',
                'DATAMARK_R2_STATE': '/r2-state', 'DATAMARK_R2_CONFIG': '/run/secrets/r2.json',
                'DATAMARK_TASKS_ENABLED': '1', 'DATAMARK_TASKS_STATE': '/task-state',
                'DATAMARK_TASKS_ARTIFACTS': '/task-artifacts'}},
            'accounts': {'image': 'datamark-r2:' + NEW, 'volumes': account_volumes}}}
        self.items = {}
        for name, volumes in [('api', api_volumes), ('accounts', account_volumes)]:
            self.items[name] = {'Config': {'Image': 'datamark-r2:' + OLD},
                                'State': {'Health': {'Status': 'healthy'}},
                                'Mounts': [{'Type': 'bind', 'Source': v['source'],
                                            'Destination': v['target'], 'RW': not v.get('read_only', False)} for v in volumes]}
        self.items['gateway'] = {'Id': 'original-gateway', 'Mounts': [
            {'Destination': '/config', 'Source': str(self.previous / 'deploy/caddy-config')}]}
        self.calls = []
        self.failure = None
        self.failed = False
        self.deployment = PublicDeployment(self.home, NEW, self.fake_run)

    def db(self, path):
        with closing(sqlite3.connect(path)) as con:
            con.execute('CREATE TABLE records(value TEXT)')
            con.execute("INSERT INTO records VALUES ('original')")
            con.commit()

    def fake_run(self, *args, capture=True):
        self.calls.append(args)
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([self.items[args[2].removeprefix('datamark-public-setup-').removesuffix('-1')]])
        if args[:2] == ('docker', 'compose'):
            if args[-3:] == ('config', '--format', 'json'):
                return json.dumps(self.rendered)
            service = args[-1]
            env = Path(args[args.index('--env-file') + 1]).read_text()
            image = next(line.split('=', 1)[1] for line in env.splitlines() if line.startswith('DATAMARK_SETUP_IMAGE='))
            if self.failure == service and image.endswith(NEW) and not self.failed:
                self.failed = True
                raise DeploymentError('injected service failure')
            self.items[service]['Config']['Image'] = image
            return ''
        if args[0] == 'curl':
            if self.failure in ('health', 'challenge'):
                with closing(sqlite3.connect(self.live)) as con:
                    con.execute("INSERT INTO records VALUES ('new live write')")
                    con.commit()
            if args[-1].endswith('/api/health'):
                return json.dumps(dict(ready=self.failure != 'health', accounts_ready=True, storage_ready=True, storage_backend='s3'))
            return json.dumps({} if self.failure == 'challenge' else dict(challenge='fixture', public_key='fixture'))
        if args[:3] == ('docker', 'exec', 'datamark-public-setup-api-1'):
            if self.failure == 'prediction':
                raise DeploymentError('fixture worker unavailable')
            return ''
        if args[:2] == ('docker', 'stop'):
            return ''
        raise AssertionError(args)

    def assert_rolled_back(self):
        self.assertEqual((self.home / 'runtime.conf').read_text(), self.config)
        self.assertEqual((self.home / 'current').resolve(), self.previous)
        for name in ('api', 'accounts'):
            self.assertEqual(self.items[name]['Config']['Image'], 'datamark-r2:' + OLD)

    def test_success_preserves_state_and_shared_accounts(self):
        self.deployment.deploy()
        self.assertEqual((self.home / 'current').resolve(), self.release)
        self.assertIn('datamark-r2:' + NEW, (self.home / 'runtime.conf').read_text())
        self.assertIn('PRESERVED_SETTING=yes', (self.home / 'runtime.conf').read_text())
        backup, = (self.home / 'backups').iterdir()
        for path in ['auth.sqlite3', 'r2-state/projects.sqlite3', 'task-state/tasks.sqlite3']:
            with closing(sqlite3.connect(backup / path)) as con:
                self.assertEqual(con.execute('SELECT value FROM records').fetchall(), [('original',)])
        self.assertEqual((backup / 'task-artifacts/result.json').read_text(), 'immutable result')
        self.assertFalse((backup / 'secrets').exists())
        ups = [c for c in self.calls if 'up' in c]
        self.assertEqual([c[-1] for c in ups], ['accounts', 'api'])
        self.assertTrue(all('--no-deps' in c and '--wait' in c for c in ups))
        self.assertEqual(self.items['gateway']['Id'], 'original-gateway')

    def test_failed_services_and_https_checks_restore_images_but_never_live_data(self):
        for failure in ('accounts', 'api', 'health', 'challenge'):
            with self.subTest(failure=failure):
                self.failure, self.failed = failure, False
                with self.assertRaises(DeploymentError):
                    self.deployment.deploy()
                self.assert_rolled_back()
        with closing(sqlite3.connect(self.live)) as con:
            self.assertGreater(con.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)

    def test_backup_failure_restarts_previous_services(self):
        with patch('deploy.ci_deploy_public.backup_state', side_effect=OSError('fixture disk full')):
            with self.assertRaises(OSError):
                self.deployment.deploy()
        self.assert_rolled_back()
        self.assertEqual([c[-1] for c in self.calls if 'up' in c], ['accounts', 'api'])

    def test_configuration_drift_fails_before_stopping_api(self):
        for kind in ('mount', 'port', 'origin', 'extra_nas', 'secret_writable'):
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.rendered)
                api = self.rendered['services']['api']
                if kind == 'mount': api['volumes'][0]['source'] = str(self.account)
                if kind == 'port': api['ports'] = [{'published': '8765'}]
                if kind == 'origin': api['environment']['DATAMARK_PUBLIC_ORIGIN'] = 'https://wrong.example'
                if kind == 'extra_nas': api['volumes'].append(dict(type='bind', source=str(self.account), target='/nas'))
                if kind == 'secret_writable': api['volumes'][-1]['read_only'] = False
                with self.assertRaises(DeploymentError): self.deployment.deploy()
                self.assertFalse(any(c[:2] == ('docker', 'stop') for c in self.calls))
                self.rendered = original

    def test_release_marker_and_image_pin_guards(self):
        self.assertEqual(prepare_release(self.home, NEW), self.release)
        (self.release / '.release-commit').write_text(OLD)
        with self.assertRaises(DeploymentError): prepare_release(self.home, NEW)
        for config in ('', self.config + self.config, self.config.replace(OLD, NEW)):
            with self.assertRaises(DeploymentError): replace_image(config, 'datamark-r2:' + OLD, 'new')

    def test_exact_remote_and_clean_tracked_source_required(self):
        for remote, dirty in ((OLD, ''), (NEW, ' M Jenkinsfile'), (NEW, '')):
            calls = []
            def run(*args):
                calls.append(args)
                if args[:2] == ('git', 'status'): return dirty
                if args[0] == 'docker': return '[]'
                return NEW if args[-1] == 'HEAD' else remote
            if remote != NEW or dirty:
                with self.assertRaises(DeploymentError): verify_repository(run)
                self.assertFalse(any(c[0] == 'docker' for c in calls))
            else: self.assertEqual(verify_repository(run), NEW)


    def enable_prediction(self):
        secret = self.home / 'secrets/prediction-token'
        secret.write_text('fixture-only-not-a-real-token')
        secret.chmod(0o600)
        self.config += 'DATAMARK_PREDICTION_EXTERNAL=1\n'
        (self.home / 'runtime.conf').write_text(self.config)
        for release in (self.previous, self.release):
            (release / 'deploy/compose.prediction-external.yaml').write_text('fixture overlay')
        self.rendered['services']['api']['environment'].update(
            DATAMARK_PREDICTION_URL='http://imu-posture-worker:8778',
            DATAMARK_PREDICTION_TOKEN_FILE='/run/secrets/imu-prediction-token')
        self.rendered['services']['api']['volumes'].append(dict(
            type='bind', source=str(secret), target='/run/secrets/imu-prediction-token', read_only=True))
        self.items['api']['Mounts'].append(dict(
            Type='bind', Source=str(secret), Destination='/run/secrets/imu-prediction-token', RW=False))
        return secret

    def test_prediction_opt_in_preserved_and_health_checked(self):
        secret = self.enable_prediction()
        with patch('deploy.ci_deploy_public.PREDICTION_SECRET', secret):
            self.deployment.deploy()
        self.assertIn('DATAMARK_PREDICTION_EXTERNAL=1', (self.home / 'runtime.conf').read_text())
        self.assertTrue(any('compose.prediction-external.yaml' in ' '.join(c) for c in self.calls))
        self.assertTrue(any(c[:2] == ('docker', 'exec') for c in self.calls))

    def test_prediction_failure_rolls_back_without_touching_worker(self):
        secret = self.enable_prediction()
        self.failure = 'prediction'
        with patch('deploy.ci_deploy_public.PREDICTION_SECRET', secret):
            with self.assertRaises(DeploymentError):
                self.deployment.deploy()
        self.assert_rolled_back()
        self.assertFalse(any(c[:3] == ('docker', 'stop', 'imu-posture-worker') for c in self.calls))

    def test_prediction_credentials_and_origin_fail_closed(self):
        secret = self.enable_prediction()
        original = copy.deepcopy(self.rendered)
        for kind in ('writable', 'origin', 'file', 'world_readable'):
            self.rendered = copy.deepcopy(original)
            api = self.rendered['services']['api']
            if kind == 'writable': api['volumes'][-1]['read_only'] = False
            if kind == 'origin': api['environment']['DATAMARK_PREDICTION_URL'] = 'https://outside.example.com'
            if kind == 'file': api['environment']['DATAMARK_PREDICTION_TOKEN_FILE'] = '/wrong'
            if kind == 'world_readable': secret.chmod(0o604)
            with self.subTest(kind=kind), patch('deploy.ci_deploy_public.PREDICTION_SECRET', secret):
                with self.assertRaises(DeploymentError):
                    self.deployment.deploy()
            self.assertFalse(any(c[:2] == ('docker', 'stop') for c in self.calls))
            secret.chmod(0o600)

    def test_invalid_or_duplicate_prediction_opt_in_rejected(self):
        for value in ('yes', '1\nDATAMARK_PREDICTION_EXTERNAL=0'):
            (self.home / 'runtime.conf').write_text(self.config + 'DATAMARK_PREDICTION_EXTERNAL=' + value + '\n')
            with self.assertRaises(DeploymentError):
                self.deployment.compose(self.previous, self.home / 'runtime.conf')
