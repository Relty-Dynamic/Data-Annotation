"""Exercise the deployment's actual online backup block with isolated databases."""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TaskDeploymentBackupTests(unittest.TestCase):
    def test_online_backup_preserves_optional_task_database(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / 'deploy/ci-deploy-intranet.sh').read_text()
        block = script.split('python3 - "$deploy_home/state" "$backup" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory(dir=root / '.tmp') as temporary:
            state = Path(temporary) / 'state'
            state.mkdir()
            for with_tasks in (False, True):
                if with_tasks:
                    (state / 'tasks/state').mkdir(parents=True)
                names = ['annotations.sqlite3', 'auth.sqlite3']
                if with_tasks:
                    names.append('tasks/state/tasks.sqlite3')
                for name in names:
                    with sqlite3.connect(state / name) as connection:
                        connection.execute('CREATE TABLE IF NOT EXISTS evidence (value TEXT)')
                        connection.execute('DELETE FROM evidence')
                        connection.execute('INSERT INTO evidence VALUES (?)', (name,))
                backup = Path(temporary) / ('with-tasks' if with_tasks else 'legacy')
                backup.mkdir()
                result = subprocess.run([sys.executable, '-c', block, str(state), str(backup)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((backup / 'tasks.sqlite3').exists(), with_tasks)
                for name in names:
                    with sqlite3.connect(backup / Path(name).name) as connection:
                        self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                        self.assertEqual(connection.execute('SELECT value FROM evidence').fetchone()[0], name)
