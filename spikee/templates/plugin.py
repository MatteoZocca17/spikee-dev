from abc import ABC, abstractmethod
from typing import overload

from spikee.templates.module import Module
from spikee.utilities.hinting import Content


class FatalPluginError(RuntimeError):
    """Abort generation instead of skipping a failed plugin entry."""


class Plugin(Module, ABC):
    # The output is an opaque reference; it must not be transformed, wrapped
    # or inserted into documents. Existing plugins retain their default behaviour.
    TERMINAL_OUTPUT: bool = False

    @abstractmethod
    @overload
    def transform(
        self,
        content: Content,
        exclude_patterns: list[str] | None = None,
        plugin_option: str = "",
    ) -> Content | list[Content]:
        pass

    @abstractmethod
    @overload
    def transform(
        self,
        text: str,
        exclude_patterns: list[str] | None = None,
        plugin_option: str = "",
    ) -> str | list[str]:
        pass
