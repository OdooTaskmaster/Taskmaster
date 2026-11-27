"""Verify the typed model port maps OpenAI-compatible answers and failures."""

import json
import traceback
from typing import Any

import pytest
import requests
from adapters.openai_compatible import (
    OpenAICompatibleClient,
    OpenAICompatibleError,
    OpenAICompatibleModelAdapter,
    OpenAICompatibleTimeout,
)
from adapters.settings import ModelSettings, Secret, load_settings
from application.errors import ErrorCategory, describe_user_failure
from application.model_port import (
    ModelFailure,
    ModelMessage,
    ModelRequest,
    user_message,
)

SECRET_VALUE: str = "sk-synthetic-0123456789abcdef"
TASK_TEXT: str = "Полный текст задачи клиента, который нельзя журналировать"
BASE_URL: str = "https://model.test/v1"
MODEL: str = "test-model"


class FakeTransport:
    """Return one canned body or raise one canned error.

    Example: FakeTransport(body={"choices": []}).chat(payload) returns the body.
    """

    def __init__(
        self,
        *,
        body: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.body: dict[str, Any] | None = body
        self.error: Exception | None = error
        self.sent: dict[str, Any] | None = None

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record the payload and return the canned outcome."""

        self.sent = payload
        if self.error is not None:
            raise self.error
        assert self.body is not None
        return self.body


class FakeResponse:
    """Return one canned HTTP response for the client tests.

    Example: FakeResponse(429, payload={"error": {"message": "slow"}}).ok is False.
    """

    def __init__(
        self,
        status_code: int = 200,
        *,
        payload: Any = None,
        text: str = "",
        json_error: Exception | None = None,
    ) -> None:
        self.status_code: int = status_code
        self.text: str = text
        self._payload: Any = payload
        self._json_error: Exception | None = json_error

    @property
    def ok(self) -> bool:
        """Return True for a 2xx or 3xx status."""

        return self.status_code < 400

    def json(self) -> Any:
        """Return the canned body or raise the canned decoding error."""

        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSession:
    """Record one HTTP call and return or raise a canned outcome.

    Example: FakeSession(FakeResponse()).calls records the posted URL.
    """

    def __init__(self, outcome: FakeResponse | Exception) -> None:
        self.outcome: FakeResponse | Exception = outcome
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.closed: bool = False

    def post(self, url: str, *, json: dict[str, Any], timeout: int) -> FakeResponse:
        """Record the request and return the canned response."""

        self.calls.append((url, json, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def close(self) -> None:
        """Mark the session as closed."""

        self.closed = True


def _settings() -> ModelSettings:
    return ModelSettings(Secret(SECRET_VALUE), BASE_URL, MODEL)


def _adapter(transport: FakeTransport) -> OpenAICompatibleModelAdapter:
    return OpenAICompatibleModelAdapter(_settings(), transport=transport)


def _failure(transport: FakeTransport) -> ModelFailure:
    with pytest.raises(ModelFailure) as raised:
        _adapter(transport).complete(ModelRequest((ModelMessage("user", "ping"),)))
    return raised.value


def _client(session: FakeSession) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        SECRET_VALUE,
        base_url=BASE_URL,
        timeout_seconds=15,
        session=session,
    )


def test_complete_maps_content_model_and_usage() -> None:
    transport = FakeTransport(
        body={
            "model": "test-model",
            "choices": [{"message": {"content": "Готово"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )

    response = _adapter(transport).complete(
        ModelRequest((ModelMessage("user", "ping"),)),
    )

    assert (response.content, response.model) == ("Готово", "test-model")
    assert response.usage == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }


def test_payload_uses_the_configured_model_and_no_secret() -> None:
    transport = FakeTransport(body={"choices": [{"message": {"content": "ok"}}]})
    request = ModelRequest(
        (ModelMessage("system", "Ты помощник"), ModelMessage("user", TASK_TEXT)),
        response_format={"type": "json_object"},
        max_tokens=64,
        temperature=0.2,
    )

    _adapter(transport).complete(request)

    payload = transport.sent
    assert payload is not None
    assert set(payload) == {
        "model",
        "messages",
        "response_format",
        "max_tokens",
        "temperature",
    }
    assert payload["model"] == MODEL
    assert payload["messages"] == [
        {"role": "system", "content": "Ты помощник"},
        {"role": "user", "content": TASK_TEXT},
    ]
    assert SECRET_VALUE not in json.dumps(payload)


def test_request_has_no_secret_or_provider_fields() -> None:
    assert set(ModelRequest.__dataclass_fields__) == {
        "messages",
        "response_format",
        "max_tokens",
        "temperature",
    }


def test_timeout_maps_to_timeout_without_the_raw_message() -> None:
    failure = _failure(FakeTransport(error=OpenAICompatibleTimeout()))

    assert (failure.category, failure.status_code) == (ErrorCategory.TIMEOUT, None)
    assert str(failure) == "timeout"


def test_provider_error_keeps_status_but_not_the_message() -> None:
    failure = _failure(
        FakeTransport(error=OpenAICompatibleError(429, f"{SECRET_VALUE} {TASK_TEXT}")),
    )

    assert (failure.category, failure.status_code) == (
        ErrorCategory.PROVIDER_ERROR,
        429,
    )
    assert str(failure) == "provider_error (HTTP 429)"
    assert SECRET_VALUE not in str(failure)
    assert TASK_TEXT not in str(failure)


def test_gateway_timeout_status_maps_to_timeout() -> None:
    failure = _failure(FakeTransport(error=OpenAICompatibleError(504, "upstream")))

    assert (failure.category, failure.status_code) == (ErrorCategory.TIMEOUT, 504)


def test_transport_error_maps_without_status() -> None:
    failure = _failure(
        FakeTransport(error=OpenAICompatibleError(0, "connection reset")),
    )

    assert (failure.category, failure.status_code) == (
        ErrorCategory.TRANSPORT_ERROR,
        None,
    )


def test_payload_omits_optional_fields() -> None:
    transport = FakeTransport(body={"choices": [{"message": {"content": "ok"}}]})

    _adapter(transport).complete(ModelRequest((ModelMessage("user", "ping"),)))

    assert transport.sent == {
        "model": MODEL,
        "messages": [{"role": "user", "content": "ping"}],
    }


def test_response_model_falls_back_to_the_settings_model() -> None:
    transport = FakeTransport(body={"choices": [{"message": {"content": "ok"}}]})

    response = _adapter(transport).complete(
        ModelRequest((ModelMessage("user", "ping"),)),
    )

    assert response.model == MODEL


def test_usage_keeps_only_integer_values() -> None:
    transport = FakeTransport(
        body={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 5, "note": "x", "flag": True, "nested": {"a": 1}},
        },
    )

    response = _adapter(transport).complete(
        ModelRequest((ModelMessage("user", "ping"),)),
    )

    assert response.usage == {"total_tokens": 5}


def test_client_posts_payload_to_the_endpoint_with_auth_header() -> None:
    session = FakeSession(
        FakeResponse(payload={"choices": [{"message": {"content": "ok"}}]}),
    )
    client = _client(session)

    body = client.chat({"model": MODEL})

    assert body == {"choices": [{"message": {"content": "ok"}}]}
    assert session.calls == [(f"{BASE_URL}/chat/completions", {"model": MODEL}, 15)]
    assert session.headers["Authorization"] == f"Bearer {SECRET_VALUE}"
    assert session.headers["Content-Type"] == "application/json"


def test_client_maps_requests_timeout() -> None:
    with pytest.raises(OpenAICompatibleTimeout):
        _client(FakeSession(requests.Timeout("slow"))).chat({"model": MODEL})


def test_client_maps_connection_error_without_status() -> None:
    with pytest.raises(OpenAICompatibleError) as raised:
        _client(FakeSession(requests.ConnectionError("down"))).chat({"model": MODEL})

    assert raised.value.status_code == 0
    assert raised.value.message == "ошибка сервиса модели"


def test_client_reads_provider_error_message() -> None:
    response = FakeResponse(429, payload={"error": {"message": "slow down"}})

    with pytest.raises(OpenAICompatibleError) as raised:
        _client(FakeSession(response)).chat({"model": MODEL})

    assert (raised.value.status_code, raised.value.message) == (
        429,
        "ошибка сервиса модели",
    )


def test_client_falls_back_to_the_text_body() -> None:
    response = FakeResponse(500, text="boom", json_error=ValueError("no json"))

    with pytest.raises(OpenAICompatibleError) as raised:
        _client(FakeSession(response)).chat({"model": MODEL})

    assert (raised.value.status_code, raised.value.message) == (
        500,
        "ошибка сервиса модели",
    )


def test_client_rejects_non_json_success() -> None:
    response = FakeResponse(200, json_error=ValueError("no json"))

    with pytest.raises(OpenAICompatibleError) as raised:
        _client(FakeSession(response)).chat({"model": MODEL})

    assert raised.value.status_code == 200


def test_client_rejects_non_object_success() -> None:
    response = FakeResponse(200, payload=["not", "an", "object"])

    with pytest.raises(OpenAICompatibleError) as raised:
        _client(FakeSession(response)).chat({"model": MODEL})

    assert raised.value.status_code == 200


def test_client_raises_on_error_body_without_choices() -> None:
    response = FakeResponse(200, payload={"error": {"message": "bad request"}})

    with pytest.raises(OpenAICompatibleError) as raised:
        _client(FakeSession(response)).chat({"model": MODEL})

    assert (raised.value.status_code, raised.value.message) == (
        200,
        "ошибка сервиса модели",
    )


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(200, json_error=ValueError("invalid JSON")),
        FakeResponse(200, payload=["invalid object"]),
    ],
)
def test_invalid_http_success_is_invalid_response_at_port(
    response: FakeResponse,
) -> None:
    adapter = OpenAICompatibleModelAdapter(
        _settings(), transport=_client(FakeSession(response)),
    )
    with pytest.raises(ModelFailure) as raised:
        adapter.complete(ModelRequest((ModelMessage("user", "ping"),)))
    assert raised.value.category == ErrorCategory.INVALID_RESPONSE


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(
            429, payload={"error": {"message": f"{SECRET_VALUE} {TASK_TEXT}"}},
        ),
        FakeResponse(
            500, text=f"{SECRET_VALUE} {TASK_TEXT}", json_error=ValueError("invalid"),
        ),
        FakeResponse(
            200, payload={"error": {"message": f"{SECRET_VALUE} {TASK_TEXT}"}},
        ),
    ],
)
def test_client_errors_do_not_expose_provider_details(response: FakeResponse) -> None:
    with pytest.raises(OpenAICompatibleError) as raised:
        _client(FakeSession(response)).chat({"model": MODEL})
    rendered = "".join(traceback.format_exception(raised.value))
    assert SECRET_VALUE not in rendered and TASK_TEXT not in rendered
    assert (
        SECRET_VALUE not in repr(raised.value) and TASK_TEXT not in raised.value.message
    )


def test_transport_exception_trace_hides_underlying_exception() -> None:
    session = FakeSession(requests.ConnectionError(f"{SECRET_VALUE} {TASK_TEXT}"))
    with pytest.raises(OpenAICompatibleError) as raised:
        _client(session).chat({"model": MODEL})
    rendered = "".join(traceback.format_exception(raised.value))
    assert SECRET_VALUE not in rendered and TASK_TEXT not in rendered


def test_client_close_closes_the_session() -> None:
    session = FakeSession(FakeResponse())

    _client(session).close()

    assert session.closed is True


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="no-choices"),
        pytest.param({"choices": []}, id="empty-choices"),
        pytest.param({"choices": ["text"]}, id="choice-is-not-a-mapping"),
        pytest.param({"choices": [{"message": {"content": None}}]}, id="null-content"),
        pytest.param({"choices": [{"message": {"content": ""}}]}, id="blank-content"),
        pytest.param({"choices": [{"message": {}}]}, id="no-content"),
    ],
)
def test_invalid_response_is_reported(body: dict[str, Any]) -> None:
    failure = _failure(FakeTransport(body=body))

    assert failure.category == ErrorCategory.INVALID_RESPONSE


def test_empty_request_is_rejected_before_the_call() -> None:
    transport = FakeTransport(body={"choices": [{"message": {"content": "ok"}}]})

    with pytest.raises(ModelFailure) as raised:
        _adapter(transport).complete(ModelRequest(()))

    assert raised.value.category == ErrorCategory.VALIDATION
    assert transport.sent is None


@pytest.mark.parametrize(
    ("environ", "category"),
    [
        pytest.param({}, ErrorCategory.MISSING_SECRET, id="no-key"),
        pytest.param(
            {"TASKMASTER_MODEL_API_KEY": "   "},
            ErrorCategory.MISSING_SECRET,
            id="blank-key",
        ),
        pytest.param(
            {"TASKMASTER_MODEL_API_KEY": SECRET_VALUE},
            ErrorCategory.MISSING_ENDPOINT,
            id="no-endpoint",
        ),
        pytest.param(
            {
                "TASKMASTER_MODEL_API_KEY": SECRET_VALUE,
                "TASKMASTER_MODEL_BASE_URL": "   ",
            },
            ErrorCategory.MISSING_ENDPOINT,
            id="blank-endpoint",
        ),
        pytest.param(
            {
                "TASKMASTER_MODEL_API_KEY": SECRET_VALUE,
                "TASKMASTER_MODEL_BASE_URL": "model.test/v1",
            },
            ErrorCategory.INVALID_ENDPOINT,
            id="relative-endpoint",
        ),
        pytest.param(
            {
                "TASKMASTER_MODEL_API_KEY": SECRET_VALUE,
                "TASKMASTER_MODEL_BASE_URL": "ftp://model.test/v1",
            },
            ErrorCategory.INVALID_ENDPOINT,
            id="non-http-endpoint",
        ),
        pytest.param(
            {
                "TASKMASTER_MODEL_API_KEY": SECRET_VALUE,
                "TASKMASTER_MODEL_BASE_URL": BASE_URL,
            },
            ErrorCategory.MISSING_MODEL,
            id="no-model",
        ),
        pytest.param(
            {
                "TASKMASTER_MODEL_API_KEY": SECRET_VALUE,
                "TASKMASTER_MODEL_BASE_URL": BASE_URL,
                "TASKMASTER_MODEL": "   ",
            },
            ErrorCategory.MISSING_MODEL,
            id="blank-model",
        ),
    ],
)
def test_incomplete_settings_are_rejected(
    environ: dict[str, str],
    category: ErrorCategory,
) -> None:
    with pytest.raises(ModelFailure) as raised:
        load_settings(environ)

    assert raised.value.category == category


def test_load_settings_reads_overrides() -> None:
    settings = load_settings(
        {
            "TASKMASTER_MODEL_API_KEY": SECRET_VALUE,
            "TASKMASTER_MODEL": MODEL,
            "TASKMASTER_MODEL_BASE_URL": BASE_URL,
            "TASKMASTER_MODEL_TIMEOUT_SECONDS": "15",
        },
    )

    assert (settings.base_url, settings.model, settings.timeout_seconds) == (
        BASE_URL,
        MODEL,
        15,
    )
    assert settings.api_key.reveal() == SECRET_VALUE


def test_any_openai_compatible_endpoint_is_configuration() -> None:
    settings = load_settings(
        {
            "TASKMASTER_MODEL_API_KEY": SECRET_VALUE,
            "TASKMASTER_MODEL_BASE_URL": "https://openrouter.ai/api/v1",
            "TASKMASTER_MODEL": "z-ai/glm-5.3-flash",
        },
    )

    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.model == "z-ai/glm-5.3-flash"


@pytest.mark.parametrize("raw", [None, "", "0", "-3", "soon"])
def test_invalid_timeout_falls_back_to_the_default(raw: str | None) -> None:
    environ = {
        "TASKMASTER_MODEL_API_KEY": SECRET_VALUE,
        "TASKMASTER_MODEL_BASE_URL": BASE_URL,
        "TASKMASTER_MODEL": MODEL,
    }
    if raw is not None:
        environ["TASKMASTER_MODEL_TIMEOUT_SECONDS"] = raw

    settings = load_settings(environ)

    assert settings.timeout_seconds == 90


def test_secret_never_prints_its_value() -> None:
    settings = _settings()

    assert str(settings.api_key) == "***"
    for text in (
        str(settings.api_key),
        repr(settings.api_key),
        repr(settings),
        str(settings),
    ):
        assert SECRET_VALUE not in text


def test_user_message_hides_internal_details() -> None:
    failure = ModelFailure(ErrorCategory.PROVIDER_ERROR, status_code=429)

    assert failure.user_message == "Сервис модели временно недоступен."
    assert "provider_error" not in failure.user_message
    assert "429" not in failure.user_message


def test_every_category_has_a_safe_user_message() -> None:
    for category in ErrorCategory:
        message = describe_user_failure(category)
        assert message
        assert category.value not in message
        assert "HTTP" not in message


def test_user_message_for_unknown_exception_is_generic() -> None:
    message = user_message(RuntimeError(f"{SECRET_VALUE} {TASK_TEXT}"))

    assert message == "Не удалось выполнить операцию."
    assert SECRET_VALUE not in message
    assert TASK_TEXT not in message
    assert "Traceback" not in message
    assert ".py" not in message
