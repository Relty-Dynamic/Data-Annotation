"""Retry index points to verified desktop cache; it never stores another media copy."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from .service import document_bytes
from .desktop_cache import sha256_file
from .session_cache import PROFILE


def index_path(service, prefix, record, digest):
    key=hashlib.sha256(document_bytes([1,prefix,record['name'],digest,'playback-480x270-256k32k-v1',PROFILE])).hexdigest()
    return service.local/'upload-pipeline-index'/(key+'.json')


def load(service,prefix,record,digest,prepared):
    path=index_path(service,prefix,record,digest)
    if not service.desktop_cache.enabled or not path.is_file():return None
    created=[]
    complete=False
    try:
        saved=json.loads(path.read_text())
        if (saved.get('raw_sha256')!=digest or saved.get('prefix')!=prefix or saved.get('source_name')!=record['name']
                or saved['bundle'].get('cache_profile')!=PROFILE or len(saved['bundle'].get('files',[]))!=1
                or saved['bundle']['files'][0]['key']!=record['output']):return None
        expected={i['key']:(i['size'],i['sha256']) for i in saved['bundle']['files']+saved['bundle']['cache_files']}
        actual={i['key']:(i['size'],i['sha256']) for i in saved['assets']}
        if expected!=actual:return None
        for item in saved['assets']:
            source=Path(item['desktop'])
            if not source.resolve().is_relative_to(service.desktop_cache.root.resolve()):return None
            if source.stat().st_size!=item['size'] or sha256_file(source)!=item['sha256']:return None
        for item in saved['assets']:
            relative=Path(item['key'])
            if relative.is_absolute() or '..' in relative.parts:return None
            destination=prepared/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            temporary=destination.with_name('.'+destination.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                try:os.link(item['desktop'],temporary)
                except OSError:shutil.copyfile(item['desktop'],temporary)
                if sha256_file(temporary)!=item['sha256']:return None
                os.replace(temporary,destination)
                created.append(destination)
            finally:temporary.unlink(missing_ok=True)
        complete=True
        return saved['bundle']
    except (OSError,ValueError,KeyError,TypeError):return None
    finally:
        if not complete:
            for item in created:item.unlink(missing_ok=True)


def save(service,prefix,record,digest,bundle,retained):
    if not service.desktop_cache.enabled:return
    assets=bundle['files']+bundle['cache_files']
    if any(i['key'] not in retained for i in assets):return
    path=index_path(service,prefix,record,digest);path.parent.mkdir(parents=True,exist_ok=True)
    value=dict(raw_sha256=digest,prefix=prefix,source_name=record['name'],bundle=bundle,assets=[{**{k:i[k] for k in ('key','size','sha256')},'desktop':str(retained[i['key']])} for i in assets])
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_bytes(document_bytes(value));os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)
