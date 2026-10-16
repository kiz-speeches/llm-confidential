"""SNP gateway: attested session, encrypted chat/stream; ordinary GPU outside TEE."""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time

import anyio
import httpx
import uvicorn
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from evidence import pack, b64

DOMAIN = b'llm-cpu-tee-v1'
GPU = 'http://llm-worker.confidential-inference.svc.cluster.local:8080'
AA = 'http://127.0.0.1:8006/aa/evidence'
MAX_BODY = 128 * 1024


def decode(value, length=None):
    try:
        data = base64.b64decode(value, validate=True)
        if length is not None and len(data) != length:
            raise ValueError()
        return data
    except (ValueError, TypeError):
        raise HTTPException(400, 'Invalid cryptographic input') from None


async def amd_bundle(report, client, cache):
    r = report['attestation_report']
    raw, t = pack(report), r['reported_tcb']
    hwid = bytes(r['chip_id'][:8]).hex()
    key = (hwid, tuple(sorted(t.items())))
    entry = cache.get(key)
    if not entry or entry['until'] < time.monotonic():
        base = 'https://kdsintf.amd.com/vcek/v1/Turin/'
        params = {'fmcSPL': t['fmc'], 'blSPL': t['bootloader'], 'teeSPL': t['tee'],
                  'snpSPL': t['snp'], 'ucodeSPL': t['microcode']}
        vcek, chain, crl = await asyncio.gather(client.get(base+hwid, params=params),
            client.get(base+'cert_chain'), client.get(base+'crl'))
        for response in (vcek, chain, crl):
            response.raise_for_status()
        from cryptography import x509
        ask = x509.load_pem_x509_certificates(chain.content)[0].public_bytes(serialization.Encoding.DER)
        entry = {'until': time.monotonic()+3600, 'vcek': b64(vcek.content),
                 'ask': b64(ask), 'crl': b64(crl.content)}
        cache[key] = entry
    return {'report': b64(raw), **{k: entry[k] for k in ('vcek', 'ask', 'crl')}}


async def bounded(request, maximum):
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > maximum:
            raise HTTPException(413, 'Request too large')
    return bytes(body)


def create_app(client, gpu_key, bundle=amd_bundle):
    if not re.fullmatch('[a-f0-9]{64}', gpu_key):
        raise ValueError('A 64-character hexadecimal GPU service key is required')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sessions, collateral = {}, {}
    lock, quote_lock = asyncio.Lock(), asyncio.Lock()
    active = 0

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/llm-api/status')
    async def health():
        try:
            response = await client.get(GPU+'/healthz', timeout=5)
            response.raise_for_status()
        except httpx.HTTPError:
            return JSONResponse({'ready': False}, status_code=503)
        return {'ready': True, 'model': 'qwen3-8b'}

    @app.post('/llm-api/attestation')
    async def attest(request: Request):
        try:
            data = json.loads(await bounded(request, 4096))
            nonce = decode(data['nonce'], 32)
            client_pub = decode(data['client_public_key'], 65)
            peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_pub)
        except (KeyError, ValueError, TypeError):
            raise HTTPException(400, 'Invalid attestation request') from None
        async with quote_lock:
            key = ec.generate_private_key(ec.SECP256R1())
            public = key.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
            sid = secrets.token_bytes(16)
            binding = hashlib.sha256(DOMAIN+b'\0'+nonce+public+client_pub+sid).hexdigest()
            try:
                response = await client.get(AA, params={'runtime_data': binding}, timeout=15)
                response.raise_for_status()
                evidence = await bundle(response.json(), client, collateral)
            except Exception:
                raise HTTPException(503, 'CPU attestation or AMD certificates unavailable') from None
            shared = key.exchange(ec.ECDH(), peer)
            def aes(direction):
                return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce,
                                  info=DOMAIN+b'/'+direction).derive(shared))
            async with lock:
                now = time.monotonic()
                for old in list(sessions):
                    if sessions[old]['expires'] < now:
                        del sessions[old]
                if len(sessions) >= 128:
                    raise HTTPException(429, 'Too many active sessions')
                sessions[sid.hex()] = {'request': aes(b'request'), 'response': aes(b'response'),
                                      'expires': now+1800, 'sequence': -1}
            return {'evidence': evidence, 'server_public_key': b64(public), 'session_id': sid.hex()}

    @app.post('/llm-api/chat')
    async def chat(request: Request):
        nonlocal active
        if request.headers.get('content-type') != 'application/octet-stream':
            raise HTTPException(415, 'Encrypted chat required')
        sid = request.headers.get('x-session', '')
        try:
            sequence = int(request.headers['x-sequence'])
            if not 0 <= sequence < 2**32:
                raise ValueError()
            iv = decode(request.headers['x-iv'], 12)
        except (KeyError, ValueError):
            raise HTTPException(400, 'Invalid encrypted chat headers') from None
        data = await bounded(request, MAX_BODY+16)
        async with lock:
            session = sessions.get(sid)
            if not session or session['expires'] <= time.monotonic():
                raise HTTPException(401, 'Request CPU attestation again')
            if sequence <= session['sequence']:
                raise HTTPException(409, 'Replayed chat request')
            try:
                clear = session['request'].decrypt(iv, data,
                    f'llm-cpu-tee-v1/request|{sid}|{sequence}|chat'.encode())
                payload = json.loads(clear)
                if (not isinstance(payload, dict) or payload.get('stream') is not True or
                    payload.get('model', 'qwen3-8b') != 'qwen3-8b' or
                    set(payload)-{'model', 'messages', 'stream', 'max_tokens', 'temperature'}):
                    raise ValueError()
            except Exception:
                raise HTTPException(400, 'Chat authentication or format invalid') from None
            session['sequence'] = sequence
            if active >= 2:
                raise HTTPException(429, 'Two requests are already running')
            active += 1
        try:
            # Explicit prototype boundary: plaintext leaves the CPU TEE here.
            upstream = await client.send(client.build_request('POST', GPU+'/v1/chat/completions',
                json=payload, headers={'Authorization': 'Bearer '+gpu_key}), stream=True)
        except httpx.HTTPError:
            active -= 1
            raise HTTPException(503, 'GPU service unavailable') from None
        except BaseException:
            active -= 1
            raise
        if upstream.status_code != 200:
            await upstream.aclose()
            active -= 1
            raise HTTPException(upstream.status_code if upstream.status_code in (400, 413, 422, 429)
                                else 503, 'GPU request failed')

        async def encrypted_stream():
            nonlocal active
            index, total = 0, 0
            def frame(clear):
                nonlocal index
                # Unique nonce per session/sequence/frame; AAD also binds order and direction.
                iv = sequence.to_bytes(4, 'big')+index.to_bytes(8, 'big')
                aad = f'llm-cpu-tee-v1/response|{sid}|{sequence}|{index}'.encode()
                ciphertext = session['response'].encrypt(iv, clear.encode(), aad)
                value = 'data: '+json.dumps({'index': index, 'ciphertext': b64(ciphertext)})+'\n\n'
                index += 1
                return value
            try:
                async for line in upstream.aiter_lines():
                    total += len(line.encode())
                    if total > 2*1024*1024:
                        raise ValueError('Stream too large')
                    if line.startswith('data: '):
                        yield frame(line+'\n\n')
                        if line == 'data: [DONE]':
                            return
                raise ValueError('Truncated GPU stream')
            except (ValueError, httpx.HTTPError):
                yield frame('data: {"error":"Model stream interrupted"}\n\n')
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                active -= 1
        return StreamingResponse(encrypted_stream(), media_type='text/event-stream',
            headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})
    return app


if __name__ == '__main__':
    client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), trust_env=False)
    uvicorn.run(create_app(client, os.environ['GPU_API_KEY']), host='0.0.0.0', port=8080,
                access_log=False)
