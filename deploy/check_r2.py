"""Check a protected R2 configuration without displaying credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid

from fastapi import HTTPException
from backend.public_setup import public_origin
from backend.r2_store import R2Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--probe', action='store_true', help='Create a unique small readback object; retain it for audit')
    parser.add_argument('--cors-origin', action='append', default=[], help='Explicitly configure exact HTTPS frontend origins')
    args = parser.parse_args()
    store = R2Store(args.config)
    store.health()
    next(store.list('daily/', delimiter='/'))
    if args.cors_origin:
        origins = [public_origin(value) for value in args.cors_origin]
        store.configure_cors(origins)
    if args.probe:
        key = '_datamark-checks/' + uuid.uuid4().hex + '/readback.json'
        data = json.dumps({'application': 'datamark', 'purpose': 'storage-readback-check'}).encode()
        store.put_if_absent(key, data)
        if hashlib.sha256(store.get(key)).digest() != hashlib.sha256(data).digest():
            raise RuntimeError('R2 readback did not match')
        # Verify a stale conditional update cannot overwrite an existing object.
        try:
            store.put(key, b'different', create=True)
        except HTTPException as error:
            if error.status_code != 409:
                raise
        else:
            raise RuntimeError('R2 conditional creation was not enforced')
        if store.get(key) != data:
            raise RuntimeError('R2 conditional write changed the probe')
        print('R2 条件创建、冲突拒绝和读回核验通过。检查对象保留：' + key)
    print('R2 桶连接及目录读取通过。' + ('跨域规则已配置。' if args.cors_origin else '未修改跨域规则。'))


if __name__ == '__main__':
    try:
        main()
    except HTTPException as error:
        raise SystemExit(error.detail) from None
    except (ValueError, OSError, RuntimeError):
        raise SystemExit('R2 检查未通过；请核对受保护配置、文件权限与网络。') from None
