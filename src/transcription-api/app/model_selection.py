from __future__ import annotations

import threading

from .config import Settings


_lock = threading.RLock()
_selected_models: dict[str, str] = {}


def configured_model(settings: Settings) -> str:
    if settings.llm_provider == "ollama":
        return settings.ollama_model
    if settings.llm_provider == "groq":
        return settings.groq_model
    return settings.openai_model


def current_model(settings: Settings) -> str:
    with _lock:
        return _selected_models.get(settings.llm_provider, configured_model(settings))


def select_model(provider: str, model: str) -> None:
    value = model.strip()
    if not value:
        raise ValueError("Model is required")
    with _lock:
        _selected_models[provider] = value
