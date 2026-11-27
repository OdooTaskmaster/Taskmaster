"""Call any OpenAI-compatible endpoint through the model port."""

from collections.abc import Mapping
from typing import Any, Protocol

import requests

from ._core import ErrorCategory, ModelFailure, ModelRequest, ModelResponse
from .settings import DEFAULT_TIMEOUT_SECONDS, ModelSettings, Secret


class OpenAICompatibleError(Exception):
    """Report a provider or transport failure without raw details.

    Example: str(OpenAICompatibleError(429)) returns
    "HTTP 429: ошибка сервиса модели".
    """

    __slots__ = ("message", "status_code")

    def __init__(self, status_code: int) -> None:
        self.status_code: int = status_code
        self.message: str = "ошибка сервиса модели"
        super().__init__(f"HTTP {status_code}: {self.message}")


class OpenAICompatibleInvalidResponse(OpenAICompatibleError):
    """Report a successful HTTP response with an invalid body."""


class OpenAICompatibleTimeout(OpenAICompatibleError):
    """Report a request that exceeded the configured timeout."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(0)


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
        except requests.Timeout:
            raise OpenAICompatibleTimeout() from None
        except requests.RequestException:
            raise OpenAICompatibleError(0) from None

        if not response.ok:
            raise OpenAICompatibleError(response.status_code)

        try:
            data: Any = response.json()
        except ValueError:
            raise OpenAICompatibleInvalidResponse(response.status_code) from None
        if not isinstance(data, dict):
            raise OpenAICompatibleInvalidResponse(response.status_code)
        error_payload: Any = data.get("error")
        if error_payload is not None and not data.get("choices"):
            raise OpenAICompatibleError(response.status_code)
        return data

    def close(self) -> None:
        """Close the HTTP session."""

        self._session.close()


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
        if isinstance(error, OpenAICompatibleInvalidResponse):
            return ModelFailure(ErrorCategory.INVALID_RESPONSE)
        if isinstance(error, OpenAICompatibleTimeout):
            return ModelFailure(ErrorCategory.TIMEOUT)
        if error.status_code in (408, 504):
            return ModelFailure(ErrorCategory.TIMEOUT, status_code=error.status_code)
        if error.status_code == 0:
            return ModelFailure(ErrorCategory.TRANSPORT_ERROR)
        return ModelFailure(ErrorCategory.PROVIDER_ERROR, status_code=error.status_code)
