"""Online opt-in only edits backed-up configuration; public containers are untouched."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deploy.ci_deploy_public import DeploymentError
from deploy.prepare_public_rules import prepare


class PreparePublicRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.nas, self.public = self.root / 'nas', self.root / 'public'
        self.public.mkdir()
        state = self.nas / 'state/tasks/state'
        state.mkdir(parents=True)
        with closing(sqlite3.connect(state / 'tasks.sqlite3')) as db, db:
            db.execute('CREATE TABLE evidence(value TEXT)')
            db.execute("INSERT INTO evidence VALUES ('existing task')")
        self.nas_config = 'DATAMARK_WEB_IMAGE_TAG=old\n'
        self.public_config = 'DATAMARK_SETUP_IMAGE=datamark-r2:old\n'
        (self.nas / 'intranet.env').write_text(self.nas_config)
        (self.public / 'runtime.conf').write_text(self.public_config)
        self.calls = []
        self.commit = 'a' * 40

    def run_command(self, *args):
        self.calls.append(args)
        if args[:3] == ('docker', 'network', 'inspect'):
            return '[]'
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([dict(Id=args[2], Config=dict(Image='unchanged'), State=dict(StartedAt='original-start'))])
        raise AssertionError(args)

    def prepare(self, **kwargs):
        return prepare(self.commit, intranet=self.nas, public=self.public, run=self.run_command, **kwargs)

    def test_preflight_does_not_change_configs_or_containers(self):
        result = self.prepare()
        self.assertFalse(result['apply'])
        self.assertEqual(self.nas_config, (self.nas / 'intranet.env').read_text())
        self.assertEqual(self.public_config, (self.public / 'runtime.conf').read_text())
        self.assertFalse((self.nas / 'backups').exists())

    def test_online_prepare_backs_up_nas_tasks_and_only_sets_exact_release_config(self):
        result = self.prepare(apply=True)
        backup = Path(result['backup'])
        self.assertEqual(self.public_config, (backup / 'runtime.conf').read_text())
        self.assertEqual(self.nas_config, (backup / 'intranet.env').read_text())
        with closing(sqlite3.connect(backup / 'intranet-tasks.sqlite3')) as db:
            self.assertEqual([('existing task',)], db.execute('SELECT value FROM evidence').fetchall())
        self.assertIn('DATAMARK_TASK_RULES_PUBLIC=1', (self.nas / 'intranet.env').read_text())
        self.assertIn('DATAMARK_PUBLIC_SKIP_COMMIT=' + self.commit, (self.public / 'runtime.conf').read_text())
        self.assertTrue(all(c[:2] == ('docker', 'inspect') or c[:3] == ('docker', 'network', 'inspect') for c in self.calls))
        self.assertEqual('original-start', json.loads((backup / 'public-containers-before.json').read_text())['api']['started_at'])

    def test_other_pending_release_preservation_is_not_overwritten(self):
        config = self.public_config + 'DATAMARK_PUBLIC_SKIP_COMMIT=' + 'b' * 40 + '\n'
        (self.public / 'runtime.conf').write_text(config)
        with self.assertRaises(DeploymentError):
            self.prepare(apply=True)
        self.assertEqual(config, (self.public / 'runtime.conf').read_text())
        self.assertEqual(self.nas_config, (self.nas / 'intranet.env').read_text())

    def test_partial_configuration_failure_restores_both_files(self):
        from deploy.ci_deploy_public import atomic_write
        fail = True
        def write(path, value):
            nonlocal fail
            if path == self.nas / 'intranet.env' and fail:
                fail = False
                raise OSError('injected write failure')
            atomic_write(path, value)
        with patch('deploy.prepare_public_rules.atomic_write', side_effect=write), self.assertRaises(OSError):
            self.prepare(apply=True)
        self.assertEqual(self.nas_config, (self.nas / 'intranet.env').read_text())
        self.assertEqual(self.public_config, (self.public / 'runtime.conf').read_text())
