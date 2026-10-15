import base64
import json
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.login_encryption import LoginEncryption


def envelope(challenge, username='worker', password='another long safe password'):
    key, iv = os.urandom(32), os.urandom(12)
    aad = ('datamark-login-v1:' + challenge['challenge']).encode()
    public = serialization.load_der_public_key(base64.b64decode(challenge['public_key']))
    wrapped = public.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=aad))
    encrypted = AESGCM(key).encrypt(iv, json.dumps({'username': username, 'password': password}).encode(), aad)
    return {'challenge': challenge['challenge'], **{name: base64.b64encode(value).decode()
        for name, value in [('key', wrapped), ('iv', iv), ('ciphertext', encrypted)]}}


class EnvelopeTests(unittest.TestCase):
    def test_randomized_unicode_roundtrip_replay_and_tampering(self):
        service = LoginEncryption()
        first = service.challenge('client')
        body = envelope(first, '名字@example.com', '密' * 1024)
        self.assertNotIn('password', body)
        self.assertEqual('密' * 1024, service.decrypt(body, 'client')['password'])
        with self.assertRaises(HTTPException):
            service.decrypt(body, 'client')
        challenge = service.challenge('client')
        self.assertNotEqual(envelope(challenge), envelope(challenge))
        body = envelope(challenge)
        body['ciphertext'] = base64.b64encode(b'changed').decode()
        with self.assertRaises(HTTPException):
            service.decrypt(body, 'client')

    def test_expiry_client_binding_capacity_and_no_plaintext_fallback(self):
        service = LoginEncryption()
        body = envelope(service.challenge('client'))
        with self.assertRaises(HTTPException):
            service.decrypt(body, 'other-client')
        with patch('backend.login_encryption.time.monotonic', return_value=float('inf')):
            with self.assertRaises(HTTPException):
                service.decrypt(body, 'client')
        with self.assertRaises(HTTPException):
            service.decrypt({'username': 'worker', 'password': 'secret'}, 'client')
        for _ in range(31):
            service.challenge('client')
        with self.assertRaises(HTTPException) as error:
            service.challenge('client')
        self.assertEqual(429, error.exception.status_code)
