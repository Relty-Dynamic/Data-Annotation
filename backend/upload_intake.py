"""Register remote media using server-owned metadata, without materializing videos."""
import hashlib
import json
from pathlib import Path
from fastapi import HTTPException

from .r2_store import content_sha
from .upload_proofs import UploadProofs, policy
from .service import document_bytes


def metadata(service, prefix, manifest, directory):
    if 'cache_profile' not in manifest:
        return None  # Legacy manifests have no complete upload-validation contract.
    with service.connection() as con:
        row = con.execute('SELECT document FROM projects WHERE source_key=?', (str(directory.resolve()),)).fetchone()
    if row:
        previous=json.loads(row[0])
        if previous.get('_r2_capture_sha256') == hashlib.sha256(document_bytes(manifest)).hexdigest():
            return {v['name']:{**previous['_sources'][v['id']]} for v in previous['videos']}
    declared=manifest.get('media_metadata')
    if isinstance(declared,dict) and set(declared)=={item['name'] for item in manifest['files']}:
        # This field is removed from client uploads and populated only after server checks.
        result={}
        for item in manifest['files']:
            info=declared[item['name']]
            if (not isinstance(info,dict) or info.get('codec')!='h264'
                    or type(info.get('duration_ms')) is not int or abs(info['duration_ms']-item['duration_ms'])>250):
                raise HTTPException(409,'已发布视频元数据不完整或不一致。')
            result[item['name']]={**info,'storage':'s3','stamp':dict(size=item['size'],mtime_ns=0,sample_sha256=item['sha256'])}
        return result
    proofs=UploadProofs(service.local/'r2-uploads.sqlite3')
    result={}
    for index,item in enumerate(manifest['files']):
        key=prefix+'/'+item['key']
        head=service.provider.head(key)
        if head['ContentLength'] != item['size'] or head.get('Metadata',{}).get('sha256') != item['sha256']:
            raise HTTPException(409,'已发布素材身份与上传清单不一致。')
        proof=proofs.get(key,head.get('ETag'),policy(manifest,index))
        info=proof.get('info') if proof else None
        if not info:
            # Pre-upgrade uploads may already have validated bytes on this server.
            # Read only that private validation cache, never download media for registration.
            root=service.local/'upload-validation'/item['sha256']
            matches=list(root.rglob(Path(item['key']).name)) if root.is_dir() else []
            matches+=list(root.rglob(item['name'])) if root.is_dir() else []
            candidate=next((p for p in matches if p.is_file() and p.stat().st_size==item['size'] and content_sha(p)==item['sha256']),None)
            if candidate is None:
                return None
            info=service.probe(candidate)
            if info['codec'] != 'h264' or abs(info['duration_ms']-item['duration_ms'])>250:
                raise HTTPException(409,'已发布视频元数据无法通过核验。')
            proofs.save(key,head.get('ETag'),policy(manifest,index),candidate,info)
        result[item['name']]={**info,'storage':'s3', 'stamp':dict(size=item['size'],mtime_ns=0,
            sample_sha256=item['sha256'],r2_etag=head.get('ETag'))}
    return result
