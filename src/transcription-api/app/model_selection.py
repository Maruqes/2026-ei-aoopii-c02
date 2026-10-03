from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .config import Settings

_lock = threading.RLock()
REASONING_EFFORTS = (
    "default",
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def current_effort(settings: Settings) -> str:
    if settings.llm_provider != "chatgpt":
        return "default"
    with _lock:
        stored = _load_selected_models(settings.llm_model_selection_file)
        value = stored.get(
            "chatgpt_effort", settings.chatgpt_reasoning_effort or "default"
        )
    if value not in REASONING_EFFORTS:
        raise ValueError("CHATGPT_REASONING_EFFORT inválido: " + value)
    return value


def select_effort(effort: str, storage_path: Path) -> None:
    if effort not in REASONING_EFFORTS:
        raise ValueError("Invalid reasoning effort")
    with _lock:
        stored = _load_selected_models(storage_path)
        stored["chatgpt_effort"] = effort
        _save_selected_models(storage_path, stored)


def configured_model(settings: Settings) -> str:
    if settings.llm_provider == "chatgpt":
        return settings.chatgpt_model
    if settings.llm_provider == "ollama":
        return settings.ollama_model
    if settings.llm_provider == "groq":
        return settings.groq_model
    return settings.openai_model


def current_model(settings: Settings) -> str:
    with _lock:
        stored_models = _load_selected_models(settings.llm_model_selection_file)
        return stored_models.get(
            settings.llm_provider,
            configured_model(settings),
        )


def select_model(provider: str, model: str, storage_path: Path) -> None:
    value = model.strip()
    if not value:
        raise ValueError("Model is required")
    with _lock:
        stored_models = _load_selected_models(storage_path)
        stored_models[provider] = value
        _save_selected_models(storage_path, stored_models)


def clear_selected_model(provider: str, storage_path: Path) -> None:
    with _lock:
        stored = _load_selected_models(storage_path)
        stored.pop(provider, None)
        _save_selected_models(storage_path, stored)


def _load_selected_models(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(provider).strip(): str(model).strip()
        for provider, model in raw.items()
        if str(provider).strip() and str(model).strip()
    }


def _save_selected_models(path: Path, models: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(models, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, path)
