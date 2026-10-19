"""Immutable contracts let independently uploaded modalities share one folder."""
from fastapi import HTTPException

from .service import document_bytes


def manifest_key(manifest):
    return manifest['prefix'] + ('/imu.manifest.json' if manifest.get('upload_kind') == 'imu' else '/manifest.json')


def identity(manifest):
    return {'prefix': manifest['prefix'], 'collector_name': manifest.get('collector_name'),
            'capture_date': manifest.get('capture_date') or manifest['prefix'].split('/')[1]}


def imu_files(manifest):
    return sorted([{'name': item.get('name', item['key']), 'sha256': item['sha256'], 'size': item['size']}
                   for item in manifest.get('attachments', [])
                   if item.get('name', item['key']).startswith('IMU/')], key=lambda item: item['name'])


def check_compatible(provider, manifest):
    incoming, files = identity(manifest), imu_files(manifest)
    if incoming['capture_date'] != manifest['prefix'].split('/')[1]:
        raise HTTPException(422, '采集日期与保存目录不一致。')
    for suffix in ('manifest.json', 'imu.manifest.json'):
        existing, _ = provider.optional_json(manifest['prefix'] + '/' + suffix)
        if not existing:
            continue
        if identity(existing) != incoming:
            raise HTTPException(409, '目标目录的采集日期或采集人不一致，未覆盖已有数据。')
        previous = imu_files(existing)
        if files and previous and files != previous:
            raise HTTPException(409, '该采集目录已有不同的 IMU 数据，不能覆盖或混合，请核对采集日期和采集人。')


def publish(provider, manifest):
    check_compatible(provider, manifest)
    # Conditional immutable writes also protect simultaneous FPV/IMU uploads
    # across processes; a process-local lock alone cannot establish this rule.
    provider.put_if_absent(manifest['prefix'] + '/capture.identity.json', document_bytes(identity(manifest)))
    files = imu_files(manifest)
    if files:
        provider.put_if_absent(manifest['prefix'] + '/imu.files.json', document_bytes(files))
    provider.put_if_absent(manifest_key(manifest), document_bytes(manifest))
