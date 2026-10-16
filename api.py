"""Small, stateless boundary around a private, loopback-only vLLM server."""
import json
import secrets
import time
import uuid
from collections import OrderedDict
from typing import Literal

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MODEL = "qwen3-8b"
MAX_BODY = 128 * 1024


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=32000)


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["qwen3-8b"] = MODEL
    messages: list[Message] = Field(min_length=1, max_length=64)
    stream: bool = False
    max_tokens: int = Field(default=256, ge=1, le=1024)
    temperature: float = Field(default=0.7, ge=0, le=2)


def create_app(backend: httpx.AsyncClient, api_key: str, usage_key: str) -> FastAPI:
    if min(len(api_key), len(usage_key)) < 32 or api_key == usage_key:
        raise ValueError("Two different keys of at least 32 characters are required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    records: OrderedDict[str, dict] = OrderedDict()
    active = 0

    def authorize(request: Request, key: str):
        if not secrets.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + key).encode()
        ):
            raise HTTPException(401, "Unauthorized")

    @app.get("/healthz")
    async def health():
        try:
            response = await backend.get("/health", timeout=2)
            response.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(503, "Model unavailable") from None
        return {"status": "ready"}

    @app.get("/v1/models")
    async def models(request: Request):
        authorize(request, api_key)
        return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}

    @app.get("/internal/usage/{request_id}")
    async def usage(request_id: str, request: Request):
        authorize(request, usage_key)
        if request_id not in records:
            raise HTTPException(404, "Not finalized, expired, or unknown")
        return records[request_id]

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        nonlocal active
        authorize(request, api_key)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "Request too large")
        try:
            data = Chat.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Invalid chat request or unsupported option") from None
        if active >= 2:
            raise HTTPException(429, "Two requests are already running")
        active += 1
        request_id = uuid.uuid4().hex
        started = time.time()
        counts = None
        final_usage = False

        def remember_usage(event):
            nonlocal counts, final_usage
            candidate = event.get("usage")
            if not isinstance(candidate, dict):
                return
            values = [candidate.get(k) for k in
                      ("prompt_tokens", "completion_tokens", "total_tokens")]
            if any(type(v) is not int or v < 0 for v in values):
                raise ValueError("Invalid backend usage")
            if values[0] + values[1] != values[2]:
                raise ValueError("Invalid backend usage sum")
            counts = dict(zip(("prompt_tokens", "completion_tokens", "total_tokens"), values))
            # A streaming final usage event has no choices; other events are partial.
            final_usage = not data.stream or event.get("choices") == []

        def finish(status):
            nonlocal active
            active -= 1
            records[request_id] = {
                "request_id": request_id, "model": MODEL, "status": status,
                "started_at": started, "finished_at": time.time(),
                "usage": counts, "usage_complete": status == "completed" and final_usage,
                "attested": False,
            }
            while len(records) > 1000:
                records.popitem(last=False)

        payload = data.model_dump()
        # Clients cannot select a model path, adapters, templates, cache salts or tools.
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        if data.stream:
            payload["stream_options"] = {
                "include_usage": True, "continuous_usage_stats": True,
            }
        try:
            upstream = await backend.send(
                backend.build_request("POST", "/v1/chat/completions", json=payload),
                stream=True,
            )
        except httpx.HTTPError:
            finish("failed")
            raise HTTPException(503, "Model unavailable") from None
        except BaseException:
            finish("aborted")
            raise
        headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
        if upstream.status_code != 200:
            await upstream.aclose()
            finish("failed")
            return JSONResponse({"error": "Model rejected request (check context length)"},
                                status_code=400 if upstream.status_code < 500 else 503,
                                headers=headers)
        if not data.stream:
            try:
                event = json.loads(await upstream.aread())
                remember_usage(event)
                if not final_usage:
                    raise ValueError("Missing usage")
            except (ValueError, httpx.HTTPError):
                finish("failed")
                return JSONResponse({"error": "Invalid model response"}, status_code=502,
                                    headers=headers)
            except BaseException:
                finish("aborted")
                raise
            else:
                finish("completed")
                return JSONResponse(event, headers=headers)
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        async def events():
            status = "aborted"
            try:
                async for line in upstream.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    if line == "data: [DONE]":
                        if not final_usage:
                            raise ValueError("Missing final usage")
                        status = "completed"
                        yield "data: [DONE]\n\n"
                        return
                    event = json.loads(line[6:])
                    if "error" in event:
                        raise ValueError("Backend error")
                    remember_usage(event)
                    yield line + "\n\n"
                raise ValueError("Truncated model stream")
            except (ValueError, httpx.HTTPError):
                status = "failed"
                yield 'data: {"error":"Model stream interrupted"}\n\n'
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                finish(status)

        headers["X-Accel-Buffering"] = "no"
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    return app
