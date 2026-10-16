"""Server-owned media proofs shared by staging, final publication and intake."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from .service import document_bytes


def policy(manifest, index):
    items = manifest['files'] + manifest.get('attachments', []) + manifest.get('cache_files', [])
    item = items[index]
    if index < len(manifest['files']):
        kind, duration = 'normal', item['duration_ms']
    elif index < len(manifest['files']) + len(manifest.get('attachments', [])):
        kind, duration = ('prediction' if item['name'].startswith('prediction/') else 'data'), None
    else:
        kind = item['kind']
        duration = next(v['duration_ms'] for v in manifest['files'] if v['name'] == item['video_name']) if kind == 'fast' else None
    return hashlib.sha256(document_bytes([1, kind, duration, item['size'], item['sha256']])).hexdigest()


class UploadProofs:
    def __init__(self, path):
        self.path = Path(path)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('CREATE TABLE IF NOT EXISTS upload_proofs (object_key TEXT, etag TEXT, policy TEXT, document TEXT, PRIMARY KEY(object_key,etag,policy))')
        self.path.chmod(0o600)

    def get(self, key, etag, contract):
        if not etag:
            return None
        with closing(sqlite3.connect(self.path)) as con:
            row = con.execute('SELECT document FROM upload_proofs WHERE object_key=? AND etag=? AND policy=?', (key, etag, contract)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, etag, contract, destination, info=None):
        if not etag:
            return
        value = dict(path=str(destination), info=info)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('INSERT OR REPLACE INTO upload_proofs VALUES (?,?,?,?)', (key, etag, contract, json.dumps(value)))
