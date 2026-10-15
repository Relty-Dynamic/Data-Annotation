"""Private R2 objects, conditional versions and short-lived authorized URLs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException


def object_key(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 1024
            or any(part in ('', '.', '..') for part in value.split('/'))
            or any(ord(char) < 32 or char in '\\:' for char in value)):
        raise HTTPException(422, '对象路径无效。')
    return value


def content_sha(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


class R2Store:
    def __init__(self, config_file: Path, *, client=None):
        if config_file.is_symlink() or not config_file.is_file():
            raise ValueError('A protected R2 configuration file is required')
        if os.name != 'nt' and config_file.stat().st_mode & 0o077:
            raise ValueError('R2 configuration must be accessible only to its owner')
        config = json.loads(config_file.read_text(encoding='utf-8'))
        parsed = urlsplit(config.get('endpoint', ''))
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.password or parsed.port is not None
                or not re.fullmatch(r'[a-f0-9]{32}\.r2\.cloudflarestorage\.com', parsed.hostname)):
            raise ValueError('Expected an account-level HTTPS R2 S3 endpoint')
        self.bucket = config.get('bucket', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', self.bucket):
            raise ValueError('Invalid R2 bucket')
        if not config.get('access_key_id') or not config.get('secret_access_key'):
            raise ValueError('R2 credentials are required')
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client('s3', endpoint_url=config['endpoint'], region_name='auto',
                aws_access_key_id=config['access_key_id'], aws_secret_access_key=config['secret_access_key'],
                config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=60,
                              retries={'max_attempts': 3, 'mode': 'standard'},
                              request_checksum_calculation='when_required', response_checksum_validation='when_required',
                              s3={'addressing_style': 'path'}))
        self.client = client

    def call(self, operation, **arguments):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return getattr(self.client, operation)(Bucket=self.bucket, **arguments)
        except ClientError as error:
            code = str(error.response.get('Error', {}).get('Code', ''))
            if code in ('NoSuchKey', 'NoSuchUpload', 'NoSuchCORSConfiguration', 'NotFound', '404'):
                raise HTTPException(404, 'R2 对象不存在。') from None
            if code in ('PreconditionFailed', 'ConditionalRequestConflict', '412', '409'):
                raise HTTPException(409, 'R2 对象版本已改变或已存在，未覆盖。') from None
            raise HTTPException(503, 'R2 请求失败，请检查存储权限和网络。') from None
        except BotoCoreError:
            raise HTTPException(503, 'R2 连接失败，请稍后重试。') from None

    def health(self):
        self.call('head_bucket')
        return True

    def head(self, key):
        return self.call('head_object', Key=object_key(key))

    def get(self, key, *, limit=32 * 1024 * 1024):
        response = self.call('get_object', Key=object_key(key))
        with response['Body'] as body:
            if response['ContentLength'] > limit:
                raise HTTPException(422, '对象过大，不能作为标注或目录清单读取。')
            result = body.read(limit + 1)
        if len(result) > limit:
            raise HTTPException(422, '对象过大。')
        return result

    def optional_json(self, key):
        try:
            head = self.head(key)
            response = self.call('get_object', Key=object_key(key), IfMatch=head['ETag'])
        except HTTPException as error:
            if error.status_code == 404:
                return None, None
            raise
        with response['Body'] as body:
            if response['ContentLength'] > 32 * 1024 * 1024:
                raise HTTPException(422, '目录清单过大。')
            try:
                result = json.loads(body.read())
            except ValueError:
                raise HTTPException(422, 'R2 目录清单格式无效。') from None
        return result, head['ETag']

    def put(self, key, payload, *, etag=None, create=False, content_type='application/json'):
        conditions = {'IfNoneMatch': '*'} if create else {'IfMatch': etag} if etag else {}
        if not conditions:
            raise ValueError('R2 writes require conditional creation or an expected version')
        return self.call('put_object', Key=object_key(key), Body=payload, ContentType=content_type,
                         Metadata={'sha256': hashlib.sha256(payload).hexdigest()}, **conditions)['ETag']

    def put_if_absent(self, key, payload):
        try:
            self.put(key, payload, create=True)
        except HTTPException as error:
            if error.status_code != 409 or self.get(key) != payload:
                raise
        if self.get(key) != payload:
            raise HTTPException(503, 'R2 对象读回校验失败。')

    def put_file(self, key, path):
        key = object_key(key)
        checksum = content_sha(path)
        try:
            existing = self.head(key)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            existing = None
        if existing:
            if existing.get('Metadata', {}).get('sha256') != checksum or existing['ContentLength'] != path.stat().st_size:
                raise HTTPException(409, 'R2 缓存已有不同内容，未覆盖。')
            return
        try:
            with path.open('rb') as source:
                self.call('put_object', Key=key, Body=source, IfNoneMatch='*',
                          ContentType='video/mp4' if path.suffix == '.mp4' else 'image/jpeg', Metadata={'sha256': checksum})
        except HTTPException as error:
            if error.status_code != 409:
                raise
            # Another request may have published this immutable cache meanwhile.
            # The size and checksum below must still match before reuse.
        head = self.head(key)
        if head['ContentLength'] != path.stat().st_size or head.get('Metadata', {}).get('sha256') != checksum:
            raise HTTPException(503, 'R2 缓存写入校验失败。')

    def download(self, key, destination, checksum, size):
        if (not isinstance(checksum, str) or not re.fullmatch(r'[a-f0-9]{64}', checksum) or type(size) is not int or not 0 < size <= 16 * 1024**3):
            raise HTTPException(422, '视频缓存清单无效。')
        if destination.is_file() and destination.stat().st_size == size and content_sha(destination) == checksum:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name('.' + destination.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            response = self.call('get_object', Key=object_key(key))
            with response['Body'] as body, temp.open('xb') as output:
                if response['ContentLength'] != size:
                    raise HTTPException(409, 'R2 视频大小与清单不一致。')
                total = 0
                while chunk := body.read(1024 * 1024):
                    total += len(chunk)
                    if total > size:
                        raise HTTPException(409, 'R2 视频超过清单大小。')
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if temp.stat().st_size != size or content_sha(temp) != checksum:
                raise HTTPException(409, 'R2 视频内容校验失败。')
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    def list(self, prefix='', delimiter=None):
        arguments = {'Prefix': prefix, 'MaxKeys': 1000}
        if delimiter:
            arguments['Delimiter'] = delimiter
        while True:
            response = self.call('list_objects_v2', **arguments)
            yield response
            if not response.get('IsTruncated'):
                break
            arguments['ContinuationToken'] = response['NextContinuationToken']

    def signed_url(self, key, *, operation='get_object', expires=1800, **parameters):
        return self.client.generate_presigned_url(operation, Params={'Bucket': self.bucket, 'Key': object_key(key), **parameters}, ExpiresIn=expires)

    def configure_cors(self, origins):
        rule = {'ID': 'datamark-authorized-browser', 'AllowedOrigins': origins,
                'AllowedMethods': ['GET', 'HEAD', 'PUT'], 'AllowedHeaders': ['Content-Type', 'Range', 'If-Match'],
                'ExposeHeaders': ['ETag', 'Content-Length', 'Content-Range', 'Accept-Ranges'], 'MaxAgeSeconds': 3600}
        try:
            previous = self.call('get_bucket_cors').get('CORSRules', [])
        except HTTPException as error:
            if error.status_code != 404:
                raise
            previous = []
        # Preserve unrelated rules; managed rule only grants the exact UI origins.
        rules = [item for item in previous if item.get('ID') != rule['ID']] + [rule]
        self.call('put_bucket_cors', CORSConfiguration={'CORSRules': rules})
