"""Define the typed boundary between core code and one external model."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import ErrorCategory, describe_failure, describe_user_failure


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """Carry one chat turn with an explicit role.

    Example: ModelMessage("user", "Сколько задач просрочено?") is a user turn.
    """

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Describe one model call without provider-specific fields.

    Example: ModelRequest((ModelMessage("user", "ping"),)) asks for a reply.
    """

    messages: Sequence[ModelMessage]
    response_format: Mapping[str, Any] | None = None
    max_tokens: int | None = None
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Carry the accepted answer and its safe metadata.

    Example: ModelResponse("Готово", "test-model") is a valid answer.
    """

    content: str
    model: str
    usage: Mapping[str, int] = field(default_factory=dict)


class ModelFailure(Exception):
    """Report a distinguishable failure without exposing the original text.

    The message is built from a category and an optional status code, so a
    provider message that repeats a prompt or a secret never reaches a log.
    Example: raise ModelFailure(ErrorCategory.TIMEOUT)
    """

    __slots__ = ("category", "status_code")

    def __init__(self, category: ErrorCategory, *, status_code: int | None = None) -> None:
        self.category: ErrorCategory = category
        self.status_code: int | None = status_code
        super().__init__(describe_failure(category, status_code))

    @property
    def user_message(self) -> str:
        """Return a UI-safe message without the category or status code."""

        return describe_user_failure(self.category)


def user_message(error: BaseException) -> str:
    """Return a UI-safe message for any failure.

    The text never includes the exception message, a secret or a stack.
    Example: user_message(RuntimeError("secret")) returns the generic
    operation-failed text without the original message.
    """

    category: ErrorCategory = (
        error.category if isinstance(error, ModelFailure) else ErrorCategory.UNEXPECTED
    )
    return describe_user_failure(category)


class ModelPort(Protocol):
    """Call one external model through a replaceable boundary.

    Example: adapter: ModelPort = OpenAICompatibleModelAdapter(settings)
    """

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one accepted answer or raise ModelFailure."""
        ...
