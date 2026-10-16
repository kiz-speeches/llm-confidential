"""Real session crypto; explicitly mocked AMD evidence and GPU inference."""
import base64
import hashlib
import json
import secrets
import unittest
from unittest.mock import AsyncMock

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

import tee


class Protocol(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.binding = None
        self.fail = False
        async def backend(request):
            if str(request.url).startswith(tee.AA):
                self.binding = request.url.params['runtime_data']
                return httpx.Response(200, json={'mock': True})
            if request.url.path == '/healthz':
                return httpx.Response(200, json={'status': 'ready'})
            self.seen.append((str(request.url), json.loads(request.content), request.headers['Authorization']))
            if self.fail:
                return httpx.Response(503)
            return httpx.Response(200, content=(
                'data: {"choices":[{"delta":{"content":"fixture text"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
                'data: [DONE]\n\n'), headers={'Content-Type': 'text/event-stream'})
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(backend))
        self.bundle = AsyncMock(return_value={'mock': True})
        self.app = tee.create_app(self.upstream, 'a'*64, self.bundle)
        self.browser = TestClient(self.app)
        self.nonce = secrets.token_bytes(32)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                        serialization.PublicFormat.UncompressedPoint)
        self.quote = self.attest().json()
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),
            base64.b64decode(self.quote['server_public_key']))
        shared = self.key.exchange(ec.ECDH(), peer)
        def aes(direction):
            return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=self.nonce,
                              info=tee.DOMAIN+b'/'+direction).derive(shared))
        self.request_key, self.response_key = aes(b'request'), aes(b'response')

    def attest(self):
        return self.browser.post('/llm-api/attestation', json={
            'nonce': tee.b64(self.nonce), 'client_public_key': tee.b64(self.public)})

    def packet(self, sequence=0, payload=None):
        iv = secrets.token_bytes(12)
        sid = self.quote['session_id']
        body = json.dumps(payload or {'messages': [{'role': 'user', 'content': 'private fixture'}],
                                     'stream': True}).encode()
        data = self.request_key.encrypt(iv, body,
            f'llm-cpu-tee-v1/request|{sid}|{sequence}|chat'.encode())
        return {'Content-Type': 'application/octet-stream', 'X-Session': sid,
                'X-Sequence': str(sequence), 'X-IV': tee.b64(iv)}, data

    def test_session_binding_and_minimal_envelope(self):
        self.assertEqual(set(self.quote), {'evidence', 'server_public_key', 'session_id'})
        binding = hashlib.sha256(tee.DOMAIN+b'\0'+self.nonce+
            base64.b64decode(self.quote['server_public_key'])+self.public+
            bytes.fromhex(self.quote['session_id'])).hexdigest()
        self.assertEqual(binding, self.binding)

    def test_encrypted_stream_and_replay_protection(self):
        headers, body = self.packet()
        response = self.browser.post('/llm-api/chat', headers=headers, content=body)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('fixture text', response.text)
        self.assertNotIn('prompt_tokens', response.text)
        lines = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        clear = ''
        for index, frame in enumerate(lines):
            self.assertEqual(frame['index'], index)
            clear += self.response_key.decrypt(bytes(4)+index.to_bytes(8, 'big'),
                base64.b64decode(frame['ciphertext']),
                f'llm-cpu-tee-v1/response|{self.quote["session_id"]}|0|{index}'.encode()).decode()
        self.assertIn('fixture text', clear)
        self.assertIn('data: [DONE]', clear)
        self.assertEqual(self.seen[0][2], 'Bearer '+'a'*64)
        self.assertEqual(self.browser.post('/llm-api/chat', headers=headers, content=body).status_code, 409)
        self.assertEqual(len(self.seen), 1)

    def test_tamper_and_sequence_substitution_never_reach_gpu(self):
        headers, body = self.packet()
        self.assertEqual(self.browser.post('/llm-api/chat', headers=headers,
            content=body[:-1]+bytes([body[-1]^1])).status_code, 400)
        headers['X-Sequence'] = '1'
        self.assertEqual(self.browser.post('/llm-api/chat', headers=headers, content=body).status_code, 400)
        self.assertEqual(self.seen, [])

    def test_no_plaintext_fallback_or_raw_api(self):
        self.assertEqual(self.browser.post('/llm-api/chat', json={'messages': []}).status_code, 415)
        headers, body = self.packet()
        headers['X-Session'] = 'missing'
        self.assertEqual(self.browser.post('/llm-api/chat', headers=headers, content=body).status_code, 401)
        for path in ('/v1/chat/completions', '/internal/usage/test', '/metrics', '/docs'):
            self.assertEqual(self.browser.get(path).status_code, 404)
        self.assertEqual(self.seen, [])

    def test_attestation_failure_never_returns_session(self):
        self.bundle.side_effect = RuntimeError('mock evidence unavailable')
        response = self.attest()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('session_id', response.json())

    def test_failed_backend_releases_slots(self):
        self.fail = True
        for sequence in range(3):
            headers, body = self.packet(sequence)
            self.assertEqual(self.browser.post('/llm-api/chat', headers=headers, content=body).status_code, 503)


if __name__ == '__main__':
    unittest.main()
