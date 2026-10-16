import asyncio
import json
import unittest

import httpx

from api import create_app

CHAT_KEY = "c" * 32
USAGE_KEY = "u" * 32
TOKENS = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}


class API(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []
        self.mode = "normal"

        async def engine(request):
            if request.url.path == "/health":
                return httpx.Response(200)
            body = json.loads(request.content)
            self.seen.append(body)
            if self.mode == "unavailable":
                raise httpx.ConnectError("offline", request=request)
            if self.mode == "rejected":
                return httpx.Response(400)
            if not body["stream"]:
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "Hello"}}],
                    "usage": TOKENS if self.mode != "bad_usage" else None})
            events = [{"choices": [{"delta": {"content": "Hello"}}], "usage": TOKENS}]
            if self.mode == "normal":
                events.append({"choices": [], "usage": TOKENS})
            text = "".join("data: " + json.dumps(x) + "\n\n" for x in events)
            if self.mode != "truncated":
                text += "data: [DONE]\n\n"
            return httpx.Response(200, text=text)

        self.backend = httpx.AsyncClient(base_url="http://127.0.0.1:8001",
                                         transport=httpx.MockTransport(engine))
        self.client = httpx.AsyncClient(
            base_url="http://api", transport=httpx.ASGITransport(
                app=create_app(self.backend, CHAT_KEY, USAGE_KEY)))

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.backend.aclose()

    async def ask(self, content="Hello", **options):
        return await self.client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": content}], **options},
            headers={"Authorization": "Bearer " + CHAT_KEY})

    async def receipt(self, response, key=USAGE_KEY):
        return await self.client.get("/internal/usage/" + response.headers["x-request-id"],
                                     headers={"Authorization": "Bearer " + key})

    async def test_usage_has_no_content_or_caller_identity(self):
        response = await self.ask("private-user-A")
        self.assertEqual(response.status_code, 200)
        record = (await self.receipt(response)).json()
        self.assertEqual(record["usage"], TOKENS)
        self.assertTrue(record["usage_complete"])
        self.assertFalse(record["attested"])
        self.assertNotIn("private-user-A", json.dumps(record))
        self.assertNotIn("Hello", json.dumps(record))

    async def test_two_users_do_not_share_messages_or_request_ids(self):
        first = await self.ask("secret-A")
        second = await self.ask("B")
        self.assertNotEqual(first.headers["x-request-id"], second.headers["x-request-id"])
        self.assertNotIn("secret-A", json.dumps(self.seen[1]))
        self.assertEqual(len(self.seen[1]["messages"]), 1)

    async def test_keys_are_separate_and_not_forwarded(self):
        response = await self.ask()
        self.assertEqual((await self.receipt(response, CHAT_KEY)).status_code, 401)
        for key in ["", USAGE_KEY]:
            response = await self.client.post("/v1/chat/completions", json={},
                                              headers={"Authorization": "Bearer " + key})
            self.assertEqual(response.status_code, 401)
        self.assertNotIn(CHAT_KEY, json.dumps(self.seen))

    async def test_runtime_overrides_and_forged_billing_identity_rejected(self):
        for extra in [{"model": "some/other-model"}, {"user": "victim"},
                      {"chat_template": "injection"}, {"tools": []},
                      {"cache_salt": "shared"}, {"max_tokens": 100000},
                      {"temperature": "0"}, {"messages": []}]:
            with self.subTest(extra=extra):
                self.assertEqual((await self.ask(**extra)).status_code, 422)
        self.assertEqual(self.seen, [])

    async def test_request_size_limit(self):
        self.assertEqual((await self.ask("x" * 140000)).status_code, 413)
        self.assertEqual(self.seen, [])

    async def test_stream_usage_and_terminator(self):
        response = await self.ask(stream=True)
        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        record = (await self.receipt(response)).json()
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["usage_complete"])
        self.assertTrue(self.seen[0]["stream_options"]["continuous_usage_stats"])

    async def test_truncated_stream_cannot_be_billed_as_complete(self):
        for mode in ["truncated", "no_final_usage"]:
            self.mode = mode
            response = await self.ask(stream=True)
            self.assertNotIn("[DONE]", response.text)
            record = (await self.receipt(response)).json()
            self.assertEqual(record["status"], "failed")
            self.assertFalse(record["usage_complete"])

    async def test_missing_usage_fails_closed(self):
        self.mode = "bad_usage"
        response = await self.ask()
        self.assertEqual(response.status_code, 502)
        self.assertFalse((await self.receipt(response)).json()["usage_complete"])

    async def test_failed_backend_releases_capacity(self):
        self.mode = "unavailable"
        for _ in range(3):
            self.assertEqual((await self.ask()).status_code, 503)
        self.mode = "rejected"
        response = await self.ask()
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.receipt(response)).json()["status"], "failed")
        self.mode = "normal"
        self.assertEqual((await self.ask()).status_code, 200)

    async def test_raw_engine_and_admin_routes_not_exposed(self):
        for path in ["/metrics", "/tokenize", "/reset_prefix_cache", "/v1/load_lora_adapter"]:
            self.assertEqual((await self.client.post(path)).status_code, 404)

    async def test_disconnect_closes_upstream_and_preserves_partial_status(self):
        closed = asyncio.Event()
        disconnect = asyncio.Event()

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield ('data: ' + json.dumps({"choices": [{"delta": {"content": "hi"}}],
                                              "usage": TOKENS}) + '\n\n').encode()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        async def engine(request):
            return httpx.Response(200, stream=SlowStream())

        async with httpx.AsyncClient(base_url="http://engine",
                                      transport=httpx.MockTransport(engine)) as backend:
            app = create_app(backend, CHAT_KEY, USAGE_KEY)
            sent = []
            received = False

            async def receive():
                nonlocal received
                if not received:
                    received = True
                    return {"type": "http.request", "body": json.dumps({
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True}).encode(), "more_body": False}
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)
                if message["type"] == "http.response.body" and message.get("body"):
                    disconnect.set()

            await asyncio.wait_for(app({
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "POST", "scheme": "http",
                "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                "query_string": b"", "root_path": "",
                "headers": [(b"authorization", ("Bearer " + CHAT_KEY).encode()),
                            (b"content-type", b"application/json")],
                "server": ("localhost", 80), "client": ("localhost", 1),
            }, receive, send), timeout=5)
            self.assertTrue(closed.is_set())
            request_id = dict(sent[0]["headers"])[b"x-request-id"].decode()
            async with httpx.AsyncClient(base_url="http://api",
                                          transport=httpx.ASGITransport(app=app)) as client:
                receipt = (await client.get("/internal/usage/" + request_id,
                    headers={"Authorization": "Bearer " + USAGE_KEY})).json()
            self.assertEqual(receipt["status"], "aborted")
            self.assertEqual(receipt["usage"], TOKENS)
            self.assertFalse(receipt["usage_complete"])


if __name__ == "__main__":
    unittest.main()
