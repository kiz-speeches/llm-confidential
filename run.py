"""Fixed engine configuration, loopback backend, supervised lifecycle."""
import asyncio
import os
from pathlib import Path
import signal

import httpx
import uvicorn

from api import create_app
from model import prepare


def engine_command():
    return [
        "vllm", "serve", "/model", "--served-model-name", "qwen3-8b",
        "--host", "127.0.0.1", "--port", "8001",
        "--dtype", "bfloat16", "--max-model-len", "8192",
        "--max-num-seqs", "2", "--gpu-memory-utilization", "0.85",
        "--no-enable-prefix-caching", "--generation-config", "vllm",
        "--no-enable-log-requests", "--no-enable-log-outputs",
        "--disable-uvicorn-access-log", "--disable-log-stats",
    ]


async def main():
    api_key, usage_key = os.environ["API_KEY"], os.environ["USAGE_API_KEY"]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001",
                                 timeout=httpx.Timeout(300, connect=5),
                                 trust_env=False) as backend:
        app = create_app(backend, api_key, usage_key)
        await asyncio.to_thread(prepare, Path("/model"))
        engine = await asyncio.create_subprocess_exec(*engine_command(), start_new_session=True)
        engine_wait = asyncio.create_task(engine.wait())
        server_task = None
        try:
            for _ in range(900):
                if engine_wait.done():
                    raise RuntimeError("Engine exited during startup")
                try:
                    if (await backend.get("/health", timeout=1)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Engine startup timed out")
            server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8080,
                                                   access_log=False, log_level="warning"))
            server_task = asyncio.create_task(server.serve())
            done, _ = await asyncio.wait([server_task, engine_wait],
                                         return_when=asyncio.FIRST_COMPLETED)
            if engine_wait in done:
                server.should_exit = True
                await server_task
                raise RuntimeError("Engine exited unexpectedly")
            await server_task
        finally:
            if server_task and not server_task.done():
                server_task.cancel()
            # Terminate the whole engine process group, including GPU workers.
            if engine.returncode is None:
                os.killpg(engine.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(engine_wait), 15)
                except asyncio.TimeoutError:
                    os.killpg(engine.pid, signal.SIGKILL)
                    await engine_wait


if __name__ == "__main__":
    asyncio.run(main())
