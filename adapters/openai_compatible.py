"""Call any OpenAI-compatible endpoint through the model port."""

from collections.abc import Mapping
from typing import Any, Protocol

import requests

from ._core import ErrorCategory, ModelFailure, ModelRequest, ModelResponse
from .settings import DEFAULT_TIMEOUT_SECONDS, ModelSettings, Secret


class OpenAICompatibleError(Exception):
    """Report a provider or transport failure with its raw details.

    Example: str(OpenAICompatibleError(429, "too many requests")) returns
    "HTTP 429: too many requests".
    """

    __slots__ = ("message", "status_code")

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code: int = status_code
        self.message: str = message
        super().__init__(f"HTTP {status_code}: {message}")


class OpenAICompatibleTimeout(OpenAICompatibleError):
    """Report a request that exceeded the configured timeout."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(0, "истекло время ожидания ответа")


class ChatTransport(Protocol):
    """Send one Chat Completions payload and return its decoded body."""

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the decoded body or raise OpenAICompatibleError."""
        ...


class OpenAICompatibleClient:
    """Send one Chat Completions request without automatic retries.

    The endpoint is supplied by the caller, so no provider is baked in.
    Example: OpenAICompatibleClient(Secret("sk-test"), base_url="https://model.test/v1")
    """

    def __init__(
        self,
        api_key: Secret | str,
        *,
        base_url: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        secret: Secret = api_key if isinstance(api_key, Secret) else Secret(api_key)
        self._base_url: str = base_url.rstrip("/")
        self._timeout: int = timeout_seconds
        self._session: requests.Session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {secret.reveal()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one Chat Completions request without retrying it."""

        try:
            response: requests.Response = self._session.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                timeout=self._timeout,
            )
        except requests.Timeout as error:
            raise OpenAICompatibleTimeout() from error
        except requests.RequestException as error:
            raise OpenAICompatibleError(0, f"транспортная ошибка: {type(error).__name__}") from error

        if not response.ok:
            message: str = _error_message(response)
            raise OpenAICompatibleError(response.status_code, message)

        try:
            data: Any = response.json()
        except ValueError as error:
            raise OpenAICompatibleError(response.status_code, "ответ не является JSON") from error
        if not isinstance(data, dict):
            raise OpenAICompatibleError(response.status_code, "неожиданный формат ответа")
        error_payload: Any = data.get("error")
        if error_payload is not None and not data.get("choices"):
            raise OpenAICompatibleError(response.status_code, _error_text(error_payload))
        return data

    def close(self) -> None:
        """Close the HTTP session."""

        self._session.close()


def _error_text(error: Any) -> str:
    """Extract a readable message from a provider error payload."""

    if isinstance(error, dict):
        message: Any = error.get("message")
        if isinstance(message, str):
            return message[:500]
    if isinstance(error, str):
        return error[:500]
    return "провайдер вернул ошибку в теле ответа"


def _error_message(response: requests.Response) -> str:
    """Extract a readable message from a failed HTTP response."""

    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error: Any = payload.get("error")
        if error is not None:
            return _error_text(error)
        message: Any = payload.get("message")
        if isinstance(message, str):
            return message[:500]
    return response.text[:500] or "пустой ответ"


class OpenAICompatibleModelAdapter:
    """Implement the model port over an OpenAI-compatible endpoint.

    Example: OpenAICompatibleModelAdapter(settings).complete(request)
    """

    def __init__(
        self,
        settings: ModelSettings,
        *,
        transport: ChatTransport | None = None,
    ) -> None:
        self._settings: ModelSettings = settings
        self._transport: ChatTransport = transport or OpenAICompatibleClient(
            settings.api_key,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one accepted answer or raise ModelFailure."""

        payload: dict[str, Any] = self._build_payload(request)
        try:
            data: dict[str, Any] = self._transport.chat(payload)
        except OpenAICompatibleError as error:
            raise self._translate(error) from None
        return self._read_response(data)

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        if not request.messages:
            raise ModelFailure(ErrorCategory.VALIDATION)
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
        }
        if request.response_format is not None:
            payload["response_format"] = dict(request.response_format)
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    def _read_response(self, data: Mapping[str, Any]) -> ModelResponse:
        choices: Any = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelFailure(ErrorCategory.INVALID_RESPONSE)
        first: Any = choices[0]
        message: Any = first.get("message") if isinstance(first, Mapping) else None
        content: Any = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise ModelFailure(ErrorCategory.INVALID_RESPONSE)
        usage: dict[str, int] = {}
        raw_usage: Any = data.get("usage")
        if isinstance(raw_usage, Mapping):
            usage = {
                str(name): value
                for name, value in raw_usage.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
        model: Any = data.get("model")
        return ModelResponse(
            content=content,
            model=model if isinstance(model, str) and model else self._settings.model,
            usage=usage,
        )

    @staticmethod
    def _translate(error: OpenAICompatibleError) -> ModelFailure:
        if isinstance(error, OpenAICompatibleTimeout):
            return ModelFailure(ErrorCategory.TIMEOUT)
        if error.status_code in (408, 504):
            return ModelFailure(ErrorCategory.TIMEOUT, status_code=error.status_code)
        if error.status_code == 0:
            return ModelFailure(ErrorCategory.TRANSPORT_ERROR)
        return ModelFailure(ErrorCategory.PROVIDER_ERROR, status_code=error.status_code)
