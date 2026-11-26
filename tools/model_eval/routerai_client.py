"""Minimal RouterAI Chat Completions client with no automatic retries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class RouterAIError(Exception):
    """A provider or transport failure that must be recorded, not hidden."""

    status_code: int
    message: str

    def __str__(self) -> str:
        return f"HTTP {self.status_code}: {self.message}"


class RouterAIClient:
    """Call the OpenAI-compatible RouterAI endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://routerai.ru/api/v1",
        timeout_seconds: int = 90,
    ) -> None:
        self._base_url: str = base_url.rstrip("/")
        self._timeout: int = timeout_seconds
        self._session: requests.Session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one Chat Completions request without retrying it."""

        try:
            response = self._session.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                timeout=self._timeout,
            )
        except requests.RequestException as error:
            raise RouterAIError(0, f"транспортная ошибка: {type(error).__name__}") from error

        if not response.ok:
            message: str = self._error_message(response)
            raise RouterAIError(response.status_code, message)

        try:
            data: Any = response.json()
        except ValueError as error:
            raise RouterAIError(response.status_code, "ответ не является JSON") from error
        if not isinstance(data, dict):
            raise RouterAIError(response.status_code, "неожиданный формат ответа")
        error_payload: Any = data.get("error")
        if error_payload is not None and not data.get("choices"):
            raise RouterAIError(response.status_code, self._error_text(error_payload))
        return data

    @staticmethod
    def _error_text(error: Any) -> str:
        """Extract a readable message from a provider error payload."""

        if isinstance(error, dict):
            message: Any = error.get("message")
            if isinstance(message, str):
                return message[:500]
        if isinstance(error, str):
            return error[:500]
        return "провайдер вернул ошибку в теле ответа"

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error: Any = payload.get("error")
            if error is not None:
                return RouterAIError._error_text(error)
            message: Any = payload.get("message")
            if isinstance(message, str):
                return message[:500]
        return response.text[:500] or "пустой ответ"

    def close(self) -> None:
        """Close the HTTP session."""

        self._session.close()
