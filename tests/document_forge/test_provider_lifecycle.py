"""Regression coverage for the provider bridge used by document planning."""

import asyncio
import gc
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from openai import AsyncOpenAI

from spikee.templates.provider import Provider


class StubProvider(Provider):
    def setup(self, model, **kwargs):
        pass


class ProviderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.provider = StubProvider()
        self.environment = patch.dict("os.environ", {"SPIKEE_API_TIMEOUT": "0.1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_completed_request_does_not_wait_for_background_task(self):
        state = {}

        async def background():
            try:
                await asyncio.sleep(0.5)
                state["finished_naturally"] = True
            finally:
                state["cleaned_up"] = True

        async def request(value):
            state["loop"] = asyncio.get_running_loop()
            state["task"] = asyncio.create_task(background())
            await asyncio.sleep(0)
            return value

        self.assertEqual(self.provider.async_call(request, value="answer"), "answer")
        self.assertNotIn("finished_naturally", state)
        self.assertFalse(state["task"].done())
        self.assertFalse(state["loop"].is_closed())
        self.provider._async_runner_cleanup()
        self.assertTrue(state["cleaned_up"])
        self.assertTrue(state["task"].cancelled())
        self.assertTrue(state["loop"].is_closed())

    def tearDown(self):
        cleanup = getattr(self.provider, "_async_runner_cleanup", None)
        if cleanup:
            cleanup()

    def test_reused_client_keeps_its_original_loop(self):
        class LoopBoundClient:
            async def request(self):
                loop = asyncio.get_running_loop()
                if not hasattr(self, "loop"):
                    self.loop = loop
                if self.loop.is_closed() or self.loop is not loop:
                    raise RuntimeError("Client event loop changed or closed")
                await asyncio.sleep(0)
                return "answer"

        client = LoopBoundClient()
        for _ in range(3):
            self.assertEqual(self.provider.async_call(client.request), "answer")

    def test_timeout_cancels_request(self):
        state = {}

        async def request():
            try:
                await asyncio.sleep(1)
            finally:
                state["cleaned_up"] = True

        with self.assertRaises(TimeoutError):
            self.provider.async_call(request)
        self.assertTrue(state["cleaned_up"])

    def test_http_client_reuses_keepalive_connection(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = AsyncOpenAI(api_key="test-only", max_retries=0)
        provider = StubProvider()
        provider.llm = SimpleNamespace(client=client)
        try:
            with patch.dict("os.environ", {"SPIKEE_API_TIMEOUT": "5"}):
                for _ in range(3):
                    response = provider.async_call(
                        client._client.get,
                        url=f"http://127.0.0.1:{server.server_port}/",
                    )
                    self.assertEqual(response.text, "ok")
        finally:
            del provider
            gc.collect()
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertTrue(client.is_closed())

    def test_exception_is_preserved_and_next_call_works(self):
        async def fail():
            raise ValueError("request failed")

        async def succeed():
            return "answer"

        with self.assertRaisesRegex(ValueError, "request failed"):
            self.provider.async_call(fail)
        self.assertEqual(self.provider.async_call(succeed), "answer")

    def test_requests_work_in_generator_threads(self):
        async def request(value):
            await asyncio.sleep(0)
            return value

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda value: self.provider.async_call(request, value=value),
                    range(4),
                )
            )
        self.assertEqual(results, list(range(4)))


if __name__ == "__main__":
    unittest.main()
