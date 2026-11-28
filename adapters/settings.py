"""Read model settings from the process environment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from ._core import ErrorCategory, ModelFailure

DEFAULT_TIMEOUT_SECONDS: int = 90
API_KEY_VARIABLE: str = "TASKMASTER_MODEL_API_KEY"
BASE_URL_VARIABLE: str = "TASKMASTER_MODEL_BASE_URL"
MODEL_VARIABLE: str = "TASKMASTER_MODEL"
TIMEOUT_VARIABLE: str = "TASKMASTER_MODEL_TIMEOUT_SECONDS"


class Secret:
    """Hold a credential that never prints its value.

    Example: str(Secret("sk-test")) returns "***".
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            message: str = "secret must not be empty"
            raise ValueError(message)
        self._value: str = value

    def reveal(self) -> str:
        """Return the raw value for the transport only."""

        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Group the resolved endpoint, model and timeout.

    The endpoint and model are mandatory, so the code never picks a provider
    on the caller's behalf. The credential is a Secret, so printing the
    settings stays safe.
    Example: ModelSettings(Secret("sk-test"), "https://model.test/v1", "glm")
    """

    api_key: Secret
    base_url: str
    model: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


def load_settings(environ: Mapping[str, str] | None = None) -> ModelSettings:
    """Build settings from environment variables and fail safely.

    The provider is not part of the code: any OpenAI-compatible endpoint can
    be supplied. Example: load_settings({"TASKMASTER_MODEL_API_KEY": "sk-test",
    "TASKMASTER_MODEL_BASE_URL": "https://model.test/v1",
    "TASKMASTER_MODEL": "glm"}) returns settings for that endpoint.
    """

    source: Mapping[str, str] = os.environ if environ is None else environ
    raw_key: str = (source.get(API_KEY_VARIABLE) or "").strip()
    if not raw_key:
        raise ModelFailure(ErrorCategory.MISSING_SECRET)
    raw_base_url: str = (source.get(BASE_URL_VARIABLE) or "").strip()
    if not raw_base_url:
        raise ModelFailure(ErrorCategory.MISSING_ENDPOINT)
    if not _is_http_url(raw_base_url):
        raise ModelFailure(ErrorCategory.INVALID_ENDPOINT)
    raw_model: str = (source.get(MODEL_VARIABLE) or "").strip()
    if not raw_model:
        raise ModelFailure(ErrorCategory.MISSING_MODEL)
    return ModelSettings(
        api_key=Secret(raw_key),
        base_url=raw_base_url,
        model=raw_model,
        timeout_seconds=_positive_int(source.get(TIMEOUT_VARIABLE), DEFAULT_TIMEOUT_SECONDS),
    )


def _is_http_url(value: str) -> bool:
    """Return True for an absolute http or https URL."""

    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _positive_int(raw: str | None, default: int) -> int:
    """Return a positive integer or the default without echoing the input."""

    if raw is None:
        return default
    try:
        value: int = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default
