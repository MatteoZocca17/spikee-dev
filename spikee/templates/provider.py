import asyncio
import gc
import os
import threading
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from spikee.templates.module import Module
from spikee.utilities.hinting import ModuleOptionsHint, get_content
from spikee.utilities.llm_message import AIMessage, Message, MessageHint

_RUNNER_CREATION_LOCK = threading.Lock()


class ProviderError(Exception):
    """Custom exception for provider-related errors."""

    def __init__(
        self,
        message,
        prompt: MessageHint = "",
        response: AIMessage | None = None,
        metadata: dict | None = None,
    ):
        super().__init__(message)
        self.prompt = prompt
        self.response = response
        self.metadata = {} if metadata is None else metadata


class Provider(Module, ABC):
    @property
    def default_timeout(self) -> float | None:
        """Global fallback for provider timeouts, reads from SPIKEE_API_TIMEOUT."""
        val = os.getenv("SPIKEE_API_TIMEOUT")
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return None

    @property
    def default_model(self) -> str | None:
        """Override in subclass to specify a default model key."""
        return None

    @property
    def models(self) -> dict | None:
        """Override in subclass to specify a mapping of user-friendly keys to actual model identifiers."""
        return None

    @property
    def logprobs_models(self) -> list[str]:
        """Override in subclass to specify which models support logprobs."""
        return []

    @property
    def debug_prompt(self) -> bool:
        """Global flag to enable debug prompt logging, reads from PROVIDER_DEBUG."""
        val = os.getenv("PROVIDER_DEBUG")
        if val:
            return val.lower() in ("1", "true", "yes")
        return False

    def get_available_option_values(self) -> ModuleOptionsHint:
        """Return supported attack options; Tuple[options (default is first), llm_required]."""
        if self.models is not None:
            return [model for model in self.models], True

        else:
            return [], True

    @abstractmethod
    def setup(
        self,
        model: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        **additional_kwargs,
    ) -> None:
        """Sets up the provider with the specified model and parameters."""

    def invoke(self, messages: MessageHint) -> AIMessage:
        """
        Invokes the provider with the given messages returning an AIMessage response, and implements management logic.

        Legacy providers
        """

        if self.debug_prompt:
            if isinstance(messages, list):
                for message in messages:
                    print(
                        f"[{self.__class__.__name__} DEBUG] Prompt: {self.__extract_content(message)}"
                    )

            else:
                print(
                    f"[{self.__class__.__name__} DEBUG] Prompt: {self.__extract_content(messages)}"
                )

        try:
            response = self._invoke(messages)
        except ProviderError:
            raise

        except Exception as e:
            raise ProviderError(
                f"A generic error occurred while invoking the provider: {e!s}",
                prompt=messages,
                response=None,
                metadata={"error_type": type(e).__name__, "error_message": str(e)},
            ) from e

        if self.debug_prompt:
            print(
                f"[{self.__class__.__name__} DEBUG] Response: {self.__extract_content(response) if response else 'No response'}"
            )

        return response

    def __extract_content(self, message):

        if isinstance(message, Message):
            message = message.content

        return get_content(message)

    def _invoke(self, messages: MessageHint) -> AIMessage:
        """Internal abstract method to invoke the provider; should be implemented by subclasses."""
        raise ProviderError(
            "The '_invoke' method must be implemented by subclasses of Provider.",
            prompt=messages,
            response=None,
            metadata={"error_type": "NotImplementedError"},
        )

    def async_call(self, fun: Callable, **params) -> Any:
        """Reuse the client's loop without waiting for unrelated background tasks."""
        # Providers commonly retain an async HTTP client between invocations.
        # Its connection pool must keep using the same event loop.
        with _RUNNER_CREATION_LOCK:
            if not hasattr(self, "_async_runner"):
                self._async_runner = asyncio.Runner()
                self._async_call_lock = threading.Lock()
                self._async_runner_cleanup = weakref.finalize(
                    self, self._async_runner.close
                )

        async def run_request() -> Any:
            try:
                return await asyncio.wait_for(
                    fun(**params), timeout=self.default_timeout
                )
            finally:
                gc.collect()

        # One client cannot run concurrently on its loop from several threads.
        # Separate provider instances retain independent runners.
        with self._async_call_lock:
            return self._async_runner.run(run_request())
