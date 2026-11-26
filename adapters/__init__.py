"""Integrate the core with Odoo and other infrastructure."""

from .openai_compatible import (
    OpenAICompatibleClient,
    OpenAICompatibleError,
    OpenAICompatibleModelAdapter,
    OpenAICompatibleTimeout,
)
from .settings import ModelSettings, Secret, load_settings

__all__ = [
    "ModelSettings",
    "OpenAICompatibleClient",
    "OpenAICompatibleError",
    "OpenAICompatibleModelAdapter",
    "OpenAICompatibleTimeout",
    "Secret",
    "load_settings",
]
