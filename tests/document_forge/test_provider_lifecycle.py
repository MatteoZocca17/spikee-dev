"""Regression coverage for the provider bridge used by document planning."""

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

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

    def test_completed_request_cancels_background_task(self):
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
        self.assertTrue(state["cleaned_up"])
        self.assertTrue(state["task"].cancelled())
        self.assertTrue(state["loop"].is_closed())

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
