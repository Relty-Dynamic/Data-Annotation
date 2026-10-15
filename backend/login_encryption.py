"""Short-lived, single-use login envelopes; HTTPS remains mandatory in deployment."""
import base64
import binascii
import json
import secrets
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class LoginEncryption:
    def __init__(self):
        self._key = None
        self._pending = {}
        self._lock = threading.Lock()

    def challenge(self, client):
        now = time.monotonic()
        with self._lock:
            self._pending = {key: value for key, value in self._pending.items() if value[1] > now}
            if len(self._pending) >= 4096 or sum(value[0] == client for value in self._pending.values()) >= 32:
                raise HTTPException(429, '登录请求过多，请稍后再试。')
            if self._key is None:
                self._key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            token = secrets.token_urlsafe(32)
            self._pending[token] = (client, now + 120)
            public = self._key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return {'challenge': token, 'public_key': base64.b64encode(public).decode(), 'expires_in': 120}

    def decrypt(self, body, client):
        error = '加密登录信息无效或已过期，请重新登录。'
        if not isinstance(body, dict) or set(body) != {'challenge', 'key', 'iv', 'ciphertext'}:
            raise HTTPException(400, error)
        token = body['challenge']
        if not isinstance(token, str) or len(token) > 100:
            raise HTTPException(400, error)
        with self._lock:
            saved = self._pending.get(token)
            if not saved or saved[0] != client or saved[1] <= time.monotonic():
                raise HTTPException(400, error)
            del self._pending[token]
            private_key = self._key
        try:
            def decoded(name, maximum):
                value = body[name]
                if not isinstance(value, str) or len(value) > maximum:
                    raise ValueError()
                return base64.b64decode(value, validate=True)
            iv = decoded('iv', 24)
            if len(iv) != 12:
                raise ValueError()
            aad = ('datamark-login-v1:' + token).encode()
            key = private_key.decrypt(decoded('key', 512), padding.OAEP(
                mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=aad))
            if len(key) != 32:
                raise ValueError()
            credentials = json.loads(AESGCM(key).decrypt(iv, decoded('ciphertext', 12000), aad))
            if (not isinstance(credentials, dict) or set(credentials) != {'username', 'password'}
                    or not isinstance(credentials['username'], str) or not 1 <= len(credentials['username']) <= 254
                    or not isinstance(credentials['password'], str) or not 1 <= len(credentials['password']) <= 1024):
                raise ValueError()
            return credentials
        except (ValueError, TypeError, InvalidTag, binascii.Error, UnicodeError):
            raise HTTPException(400, error) from None


def install_login_encryption(app, login):
    envelopes = LoginEncryption()

    @app.get('/api/auth/login-challenge')
    def challenge(request: Request):
        result = envelopes.challenge(request.client.host if request.client else 'unknown')
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.post('/api/auth/login-encrypted')
    def encrypted_login(body: dict, request: Request):
        credentials = envelopes.decrypt(body, request.client.host if request.client else 'unknown')
        # The existing host callback keeps password verification, lockout and cookies identical.
        return login(credentials, request)
