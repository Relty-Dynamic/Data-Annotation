"""Content-verified, atomic playback archives in a source-adjacent NAS folder."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import zipfile
from pathlib import Path

from fastapi import HTTPException

README = '''# DataMark reusable NAS playback packages

This managed folder keeps verified ZIP packages and their JSON identity records.
Packages contain compact playback video, fast playback, and thumbnail sheets.
They are not original-video backups and do not contain annotation documents.
Desktop preparation checks local playback first, then reuses these NAS packages.
Deleting a desktop project does not delete these packages, originals, or annotations.
The worker removes its own temporary media only after NAS publication is verified.
Do not place personal files in this folder. Keep each ZIP with its matching JSON.
'''


def sha256(path: Path, stopping=None) -> str:
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            _check_stop(stopping)
            value.update(block)
    return value.hexdigest()


def _check_stop(stopping):
    if stopping is not None and stopping.is_set():
        raise HTTPException(503, 'Worker stopped; completed server files were retained for retry.')


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def same_stamp(left, right) -> bool:
    return (isinstance(left, dict) and isinstance(right, dict)
            and type(left.get('size')) is int and type(left.get('mtime_ns')) is int
            and left['size'] == right.get('size')
            and type(right.get('mtime_ns')) is int
            and abs(left['mtime_ns'] - right['mtime_ns']) < 1_000_000
            and left.get('sample_sha256') == right.get('sample_sha256'))


class NasArchiveCache:
    def __init__(self, profile: str, protocol: int):
        self.profile = profile
        self.protocol = protocol

    def paths(self, source: Path, ident: str, *, create=False) -> tuple[Path, Path]:
        if not re.fullmatch('[0-9a-f]{64}', ident):
            raise HTTPException(422, 'Invalid NAS cache identity.')
        parent = source.parent
        if _linked(parent) or parent.resolve(strict=True) != parent:
            raise HTTPException(409, 'NAS source directory contains a link; cache access was refused.')
        directory = parent
        for name in ('.datamark-cache', 'remote-v1'):
            child = directory / name
            if _linked(child) or child.resolve().parent != directory.resolve():
                raise HTTPException(409, 'NAS cache contains a link; cache access was refused.')
            if child.exists() and not child.is_dir():
                raise HTTPException(409, 'NAS cache directory is not a directory.')
            if create:
                child.mkdir(exist_ok=True)
            directory = child
        paths = (directory / (ident + '.zip'), directory / (ident + '.json'))
        for path in paths:
            self._check_file(path, directory)
        if create:
            readme = directory / 'README.md'
            self._check_file(readme, directory)
            try:
                with readme.open('x', encoding='utf-8') as handle:
                    handle.write(README)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                self._check_file(readme, directory)
        return paths

    @staticmethod
    def _check_file(path: Path, directory: Path):
        if (_linked(path) or path.resolve().parent != directory.resolve()
                or (path.exists() and not path.is_file())):
            raise HTTPException(409, 'NAS cache artifact contains a link or invalid file.')

    def _valid_metadata(self, record, job):
        archive = record.get('archive', {}) if isinstance(record, dict) else {}
        return (record.get('protocol') == self.protocol and record.get('profile') == self.profile
                and record.get('id') == job['id'] and record.get('source_path') == job['path']
                and same_stamp(record.get('source_stamp'), job['stamp'])
                and record.get('duration_ms') == job['media']['duration_ms']
                and record.get('media_start_seconds') == job['media'].get('media_start_seconds', 0)
                and type(archive.get('size')) is int and 0 < archive['size'] <= 16 * 1024 ** 3
                and isinstance(archive.get('sha256'), str)
                and re.fullmatch('[0-9a-f]{64}', archive['sha256']))

    def validate_archive(self, path: Path, job: dict, expected: dict, stopping=None) -> bool:
        try:
            if path.stat().st_size != expected['size'] or sha256(path, stopping) != expected['sha256']:
                return False
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo('manifest.json')
                if info.file_size > 8 * 1024 * 1024:
                    return False
                manifest = json.loads(archive.read(info))
            return (manifest['protocol'] == self.protocol and manifest['profile'] == self.profile
                    and same_stamp(manifest['source_stamp'], job['stamp'])
                    and manifest['duration_ms'] == job['media']['duration_ms']
                    and manifest['media_start_seconds'] == job['media'].get('media_start_seconds', 0))
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
            return False

    def read(self, job: dict, stopping=None) -> dict | None:
        archive, metadata = self.paths(Path(job['path']), job['id'])
        try:
            if metadata.stat().st_size > 65536:
                return None
            record = json.loads(metadata.read_bytes())
            if not self._valid_metadata(record, job):
                return None
            if not self.validate_archive(archive, job, record['archive'], stopping):
                return None
            self.paths(Path(job['path']), job['id'])
            return {**record['archive'], 'mtime_ns': archive.stat().st_mtime_ns}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    @staticmethod
    def _sync_directory(directory: Path):
        if os.name == 'nt':
            return
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def publish(self, job: dict, local: Path, stopping, verify_source) -> dict:
        expected = job['archive']
        if not self.validate_archive(local, job, expected, stopping):
            raise HTTPException(422, 'Server cache failed validation; it was not published to NAS.')
        archive, metadata = self.paths(Path(job['path']), job['id'], create=True)
        nonce = uuid.uuid4().hex
        temporary = archive.with_name(archive.name + '.' + nonce + '.partial')
        marker = metadata.with_name(metadata.name + '.' + nonce + '.partial')
        try:
            digest = hashlib.sha256()
            size = 0
            with local.open('rb') as incoming, temporary.open('xb') as outgoing:
                for block in iter(lambda: incoming.read(1024 * 1024), b''):
                    _check_stop(stopping)
                    outgoing.write(block)
                    digest.update(block)
                    size += len(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if size != expected['size'] or digest.hexdigest() != expected['sha256']:
                raise HTTPException(409, 'Server cache changed during NAS publication; local files were retained.')
            if not self.validate_archive(temporary, job, expected, stopping):
                raise HTTPException(422, 'NAS cache copy failed validation; local files were retained.')
            record = {'protocol': self.protocol, 'profile': self.profile, 'id': job['id'],
                      'source_path': job['path'], 'source_stamp': job['stamp'],
                      'duration_ms': job['media']['duration_ms'],
                      'media_start_seconds': job['media'].get('media_start_seconds', 0),
                      'archive': {key: expected[key] for key in ('size', 'sha256')}}
            with marker.open('x', encoding='utf-8') as handle:
                json.dump(record, handle, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            _check_stop(stopping)
            verify_source()
            self.paths(Path(job['path']), job['id'])
            os.replace(temporary, archive)
            self._sync_directory(archive.parent)
            os.replace(marker, metadata)
            self._sync_directory(metadata.parent)
            confirmed = self.read(job, stopping)
            if confirmed is None:
                raise HTTPException(422, 'NAS cache publication could not be confirmed; local files were retained.')
            verify_source()
            return confirmed
        finally:
            # Only unique unpublished files from this call can be removed.
            self.paths(Path(job['path']), job['id'])
            for path in (temporary, marker):
                self._check_file(path, archive.parent)
                path.unlink(missing_ok=True)
