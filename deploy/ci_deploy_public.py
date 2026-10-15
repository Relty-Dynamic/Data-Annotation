#!/usr/bin/env python3
"""Publish the tested feat/s3-mock image to the existing public R2 service."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import tempfile


HOME = Path('/home/relty/services/datamark-public-setup')
FILES = ('compose.public-setup.yaml', 'compose.tasks-public.yaml', 'compose.r2-public.yaml')


class DeploymentError(RuntimeError):
    pass


def command(*args, capture=True):
    environment = dict(os.environ)
    # An inherited override must not defeat the version pinned in runtime.conf.
    environment.pop('DATAMARK_SETUP_IMAGE', None)
    result = subprocess.run(args, text=True, env=environment,
                            stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE)
    if result.returncode:
        # Captured configuration and unexpected command output stay out of CI logs.
        raise DeploymentError(f'{args[0]} failed with exit code {result.returncode}')
    return result.stdout or ''


def atomic_write(path, text):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def point_current(current, release):
    temporary = current.with_name('.current-public-' + str(os.getpid()))
    try:
        temporary.symlink_to(release)
        temporary.replace(current)
    finally:
        temporary.unlink(missing_ok=True)


def replace_image(text, old, new):
    lines = text.splitlines(keepends=True)
    matching = [i for i, line in enumerate(lines) if line.startswith('DATAMARK_SETUP_IMAGE=')]
    if len(matching) != 1 or lines[matching[0]].strip() != 'DATAMARK_SETUP_IMAGE=' + old:
        raise DeploymentError('persistent image setting does not match running public containers')
    lines[matching[0]] = 'DATAMARK_SETUP_IMAGE=' + new + '\n'
    return ''.join(lines)


def verify_repository(run=command):
    commit = run('git', 'rev-parse', '--verify', 'HEAD').strip()
    remote = run('git', 'rev-parse', '--verify', 'refs/remotes/origin/feat/s3-mock').strip()
    if not re.fullmatch('[0-9a-f]{40}', commit) or commit != remote:
        raise DeploymentError('only the exact origin/feat/s3-mock commit may deploy')
    if run('git', 'status', '--porcelain', '--untracked-files=no').strip():
        raise DeploymentError('tracked source files have changed')
    run('docker', 'image', 'inspect', 'datamark-r2:' + commit)
    return commit


def prepare_release(home, commit, run=command):
    releases = home / 'releases'
    release = releases / ('r2-' + commit)
    marker = release / '.release-commit'
    if release.exists():
        if release.is_symlink() or not marker.is_file() or marker.read_text().strip() != commit:
            raise DeploymentError('existing public release is incomplete or has the wrong commit')
        return release
    staging = Path(tempfile.mkdtemp(prefix='.public-release-', dir=releases))
    try:
        archive = staging / '.source.tar'
        run('git', 'archive', '--format=tar', '--output=' + str(archive), commit)
        with tarfile.open(archive) as source:
            source.extractall(staging, filter='data')
        archive.unlink()
        (staging / '.release-commit').write_text(commit + '\n')
        staging.rename(release)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return release


def backup_state(roots, backup, account_state):
    """The public API is stopped; keep full private state, including immutable files."""
    for name, source in roots.items():
        shutil.copytree(source, backup / name, symlinks=True)
    databases = [p for name in roots for p in (backup / name).rglob('*.sqlite3')]
    for database in databases:
        if database.is_symlink():
            raise DeploymentError('a state database must not be a symbolic link')
        with closing(sqlite3.connect(database)) as con:
            if con.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise DeploymentError('public state backup failed integrity verification')
    # NAS can continue writing the shared account DB; use SQLite's online backup.
    with closing(sqlite3.connect(f'file:{account_state / "auth.sqlite3"}?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup / 'auth.sqlite3')) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise DeploymentError('shared account backup failed integrity verification')
    os.chmod(backup / 'auth.sqlite3', 0o600)


class PublicDeployment:
    def __init__(self, home, commit, run=command):
        self.home, self.commit, self.run = home, commit, run
        self.image = 'datamark-r2:' + commit
        self.current = home / 'current'
        self.env = home / 'runtime.conf'

    def inspect(self, service):
        return json.loads(self.run('docker', 'inspect', 'datamark-public-setup-' + service + '-1'))[0]

    def compose(self, release, env):
        return ['docker', 'compose', '--env-file', str(env),
                *[item for name in FILES for item in ('-f', str(release / 'deploy' / name))]]

    def validate(self, rendered, old):
        services = rendered['services']
        gateway = services['gateway']
        ports = gateway.get('ports', [])
        if len(ports) != 1 or str(ports[0].get('published')) != '8443' or not ports[0].get('host_ip'):
            raise DeploymentError('public deployment must retain its dedicated 8443 ingress')
        self.bind_ip = ports[0]['host_ip']
        for name in ('api', 'accounts'):
            service = services[name]
            if service['image'] != self.image or service.get('ports'):
                raise DeploymentError('public image or private-service port configuration is invalid')
            planned = {(str(Path(v['source']).resolve()), v['target'], v.get('read_only', False))
                       for v in service.get('volumes', []) if v['type'] == 'bind'}
            running = {(str(Path(v['Source']).resolve()), v['Destination'], not v['RW'])
                       for v in old[name]['Mounts'] if v['Type'] == 'bind'}
            if planned != running:
                raise DeploymentError('public persistent mounts must remain unchanged')
        api = services['api']
        settings = api['environment']
        self.api_origin = settings.get('DATAMARK_PUBLIC_API_ORIGIN')
        self.frontend = settings.get('DATAMARK_PUBLIC_ORIGIN')
        if self.api_origin != 'https://api-annotate.reltydynamic.com:8443' or self.frontend != 'https://datamark.reltydynamic.com':
            raise DeploymentError('unexpected public origins')
        if (settings.get('DATAMARK_R2_STATE') != '/r2-state' or str(settings.get('DATAMARK_TASKS_ENABLED')) != '1'
                or settings.get('DATAMARK_R2_CONFIG') != '/run/secrets/r2.json'
                or settings.get('DATAMARK_TASKS_STATE') != '/task-state'
                or settings.get('DATAMARK_TASKS_ARTIFACTS') != '/task-artifacts'):
            raise DeploymentError('existing public R2 and task features must stay enabled')
        allowed = {'/run/secrets/r2.json', '/r2-state', '/task-state', '/task-artifacts'}
        volumes = api.get('volumes', [])
        if {v['target'] for v in volumes} != allowed:
            raise DeploymentError('public API must not mount NAS or shared account data')
        for volume in volumes:
            source = Path(volume['source']).resolve()
            if not source.is_relative_to(self.home.resolve()) or not source.exists():
                raise DeploymentError('public state or protected credential path is invalid')
        if not next(v for v in volumes if v['target'] == '/run/secrets/r2.json').get('read_only'):
            raise DeploymentError('R2 credentials must remain mounted read-only')
        targets = {'/r2-state': 'r2-state', '/task-state': 'task-state', '/task-artifacts': 'task-artifacts'}
        self.state_roots = {targets[v['target']]: Path(v['source']) for v in volumes if v['target'] in targets}
        if any(not path.is_dir() for path in self.state_roots.values()):
            raise DeploymentError('public persistent directories are missing')
        account_volumes = services['accounts'].get('volumes', [])
        if len(account_volumes) != 1 or account_volumes[0]['target'] != '/accounts/.local':
            raise DeploymentError('shared account mount is invalid')
        self.account_state = Path(account_volumes[0]['source'])
        if not (self.account_state / 'auth.sqlite3').is_file():
            raise DeploymentError('existing shared account database is missing')

    def check_health(self):
        args = ['curl', '--noproxy', '*', '--fail', '--silent', '--show-error', '--max-time', '20',
                '--resolve', 'api-annotate.reltydynamic.com:8443:' + self.bind_ip]
        health = json.loads(self.run(*args, self.api_origin + '/api/health'))
        if any(health.get(key) is not True for key in ('ready', 'accounts_ready', 'storage_ready')) or health.get('storage_backend') != 's3':
            raise DeploymentError('public HTTPS health check failed')
        challenge = json.loads(self.run(*args, self.api_origin + '/api/auth/login-challenge'))
        if not challenge.get('challenge') or not challenge.get('public_key'):
            raise DeploymentError('encrypted public login is unavailable')
        for name in ('api', 'accounts'):
            item = self.inspect(name)
            if item['Config']['Image'] != self.image or item['State'].get('Health', {}).get('Status') != 'healthy':
                raise DeploymentError('public service is not healthy on the tested image')

    def deploy(self):
        previous = self.current.resolve()
        if not self.current.is_symlink() or previous.parent != self.home / 'releases':
            raise DeploymentError('current public release link is invalid')
        if not all((previous / 'deploy' / name).is_file() for name in FILES):
            raise DeploymentError('previous public compose files are missing')
        old = {name: self.inspect(name) for name in ('api', 'accounts')}
        old_image = old['api']['Config']['Image']
        if not old_image.startswith('datamark-r2:') or old['accounts']['Config']['Image'] != old_image:
            raise DeploymentError('unexpected or inconsistent running public images')
        old_config = self.env.read_text()
        new_config = replace_image(old_config, old_image, self.image)
        release = prepare_release(self.home, self.commit, self.run)
        release_env = release / 'runtime.conf'
        atomic_write(release_env, new_config)
        # We never recreate the gateway; retain its current private state directory.
        gateway = self.inspect('gateway')
        config_mounts = [v for v in gateway['Mounts'] if v['Destination'] == '/config']
        if len(config_mounts) != 1:
            raise DeploymentError('gateway state directory is missing')
        gateway_config = Path(config_mounts[0]['Source']).resolve()
        config_link = release / 'deploy/caddy-config'
        if not config_link.exists():
            config_link.symlink_to(gateway_config)
        elif config_link.resolve() != gateway_config:
            raise DeploymentError('gateway state path would change')
        command = self.compose(release, release_env)
        self.validate(json.loads(self.run(*command, 'config', '--format', 'json')), old)
        backups = self.home / 'backups'
        backups.mkdir(exist_ok=True, mode=0o700)
        backup = Path(tempfile.mkdtemp(prefix=self.commit + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-', dir=backups))
        shutil.copy2(self.env, backup / 'runtime.conf')
        (backup / 'previous-release').write_text(str(previous) + '\n')
        old_command = self.compose(previous, backup / 'runtime.conf')
        try:
            self.run('docker', 'stop', '--time', '30', 'datamark-public-setup-api-1', capture=False)
            backup_state(self.state_roots, backup, self.account_state)
            print('Public state and shared accounts backed up: ' + str(backup), flush=True)
            for name in ('accounts', 'api'):
                self.run(*command, 'up', '-d', '--no-build', '--no-deps', '--wait', '--wait-timeout', '180', name, capture=False)
            self.check_health()
            if self.inspect('gateway')['Id'] != gateway['Id']:
                raise DeploymentError('public gateway unexpectedly changed')
            atomic_write(self.env, new_config)
            point_current(self.current, release)
        except BaseException:
            print('Public deployment failed; restoring previous images and configuration. Live databases will not be overwritten.', flush=True)
            failures = []
            for action in (lambda: atomic_write(self.env, old_config), lambda: point_current(self.current, previous),
                           *(lambda name=name: self.run(*old_command, 'up', '-d', '--no-build', '--no-deps', '--wait', '--wait-timeout', '180', name, capture=False) for name in ('accounts', 'api'))):
                try:
                    action()
                except BaseException as error:
                    failures.append(type(error).__name__)
            if failures:
                print('Public rollback needs manual inspection: ' + ', '.join(failures), flush=True)
            raise
        print(f'Public S3 deployed commit {self.commit}; database backup: {backup}', flush=True)


def main():
    import fcntl
    if getpass.getuser() != 'relty':
        raise DeploymentError('run on relty-server as relty')
    with (HOME / '.public-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        PublicDeployment(HOME, verify_repository()).deploy()


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise DeploymentError('public deployment interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        main()
    except Exception as error:
        raise SystemExit('DataMark public deployment failed: ' + str(error))
