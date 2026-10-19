"""Back up and opt NAS into the existing public rule API without restarting public."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from deploy.ci_deploy_public import HOME, DeploymentError, atomic_write, command

INTRANET = Path('/home/relty/services/datamark-web')


def settings(text, updates):
    for key in updates:
        if sum(line.startswith(key + '=') for line in text.splitlines()) > 1:
            raise DeploymentError('duplicate persistent rule integration setting')
    lines = [line for line in text.splitlines() if not any(line.startswith(key + '=') for key in updates)]
    return '\n'.join([*lines, *(f'{key}={value}' for key, value in updates.items())]) + '\n'


def prepare(commit, *, intranet=INTRANET, public=HOME, run=command, apply=False):
    import fcntl
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise DeploymentError('an exact release commit is required')
    with ExitStack() as stack:
        for path in (intranet / '.manual-deploy.lock', public / '.public-deploy.lock'):
            lock = stack.enter_context(path.open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run('docker', 'network', 'inspect', 'datamark-public-setup_private')
        before = {}
        for name in ('api', 'accounts', 'gateway'):
            item = json.loads(run('docker', 'inspect', 'datamark-public-setup-' + name + '-1'))[0]
            before[name] = {'id': item['Id'], 'image': item['Config']['Image'],
                            'started_at': item['State']['StartedAt']}
        paths = (intranet / 'intranet.env', public / 'runtime.conf')
        originals = [path.read_text() for path in paths]
        existing = [line.split('=', 1)[1] for line in originals[1].splitlines()
                    if line.startswith('DATAMARK_PUBLIC_SKIP_COMMIT=')]
        if existing not in ([], [commit]):
            raise DeploymentError('another release already has a public preservation setting')
        updated = (settings(originals[0], {'DATAMARK_TASK_RULES_PUBLIC': '1'}),
                   settings(originals[1], {'DATAMARK_PUBLIC_SKIP_COMMIT': commit}))
        if not apply:
            return {'commit': commit, 'public_containers_preserved': before, 'apply': False}
        backups = intranet / 'backups'
        backups.mkdir(mode=0o700, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='public-rules-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-', dir=backups))
        backup.chmod(0o700)
        for name, path in zip(('intranet.env', 'runtime.conf'), paths):
            shutil.copy2(path, backup / name)
            (backup / name).chmod(0o600)
        source = intranet / 'state/tasks/state/tasks.sqlite3'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
            with closing(sqlite3.connect(backup / 'intranet-tasks.sqlite3')) as saved:
                db.backup(saved)
                if saved.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise DeploymentError('NAS task backup failed integrity verification')
        (backup / 'intranet-tasks.sqlite3').chmod(0o600)
        atomic_write(backup / 'public-containers-before.json', json.dumps(before))
        try:
            # Preserve public first, before opting NAS into its rule API.
            atomic_write(paths[1], updated[1])
            atomic_write(paths[0], updated[0])
        except BaseException:
            for path, original in zip(paths, originals):
                atomic_write(path, original)
            raise
        return {'commit': commit, 'backup': str(backup), 'public_containers_preserved': before, 'apply': True}


def main():
    import argparse
    import getpass
    if getpass.getuser() != 'relty':
        raise DeploymentError('run on relty-server as relty')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(prepare(args.commit, apply=args.apply)), flush=True)


if __name__ == '__main__':
    main()
