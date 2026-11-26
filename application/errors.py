"""Classify failures for safe diagnostics."""

from enum import StrEnum


class ErrorCategory(StrEnum):
    """Name a failure without exposing the original message.

    Example: ErrorCategory.TIMEOUT identifies a provider timeout.
    """

    MISSING_SECRET = "missing_secret"
    MISSING_ENDPOINT = "missing_endpoint"
    INVALID_ENDPOINT = "invalid_endpoint"
    MISSING_MODEL = "missing_model"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER_ERROR = "provider_error"
    TRANSPORT_ERROR = "transport_error"
    VALIDATION = "validation"
    UNEXPECTED = "unexpected"


def describe_failure(category: ErrorCategory, status_code: int | None = None) -> str:
    """Return a short diagnostic that never contains the original text.

    Example: describe_failure(ErrorCategory.PROVIDER_ERROR, 429)
    returns "provider_error (HTTP 429)".
    """

    if status_code is None:
        return category.value
    return f"{category.value} (HTTP {status_code})"


_USER_MESSAGES: dict[ErrorCategory, str] = {
    ErrorCategory.MISSING_SECRET: "Не настроен ключ доступа к модели.",
    ErrorCategory.MISSING_ENDPOINT: "Не настроен адрес модели.",
    ErrorCategory.INVALID_ENDPOINT: "Адрес модели настроен неверно.",
    ErrorCategory.MISSING_MODEL: "Не выбрана модель.",
    ErrorCategory.TIMEOUT: "Модель не ответила вовремя. Повторите позже.",
    ErrorCategory.INVALID_RESPONSE: "Модель вернула неожиданный ответ.",
    ErrorCategory.PROVIDER_ERROR: "Сервис модели временно недоступен.",
    ErrorCategory.TRANSPORT_ERROR: "Не удалось связаться с сервисом модели.",
    ErrorCategory.VALIDATION: "Запрос к модели составлен неверно.",
    ErrorCategory.UNEXPECTED: "Не удалось выполнить операцию.",
}


def describe_user_failure(category: ErrorCategory) -> str:
    """Return a UI-safe message for one failure category.

    The text never includes a provider message, a secret or a stack.
    Example: describe_user_failure(ErrorCategory.TIMEOUT) returns
    "Модель не ответила вовремя. Повторите позже."
    """

    return _USER_MESSAGES[category]
