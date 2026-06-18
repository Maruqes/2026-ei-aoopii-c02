from __future__ import annotations

import json
import os
import threading
from pathlib import Path

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
        stored_models = _load_selected_models(settings.llm_model_selection_file)
        return stored_models.get(
            settings.llm_provider,
            _selected_models.get(settings.llm_provider, configured_model(settings)),
        )


def select_model(provider: str, model: str, storage_path: Path | None = None) -> None:
    value = model.strip()
    if not value:
        raise ValueError("Model is required")
    with _lock:
        _selected_models[provider] = value
        if storage_path is not None:
            stored_models = _load_selected_models(storage_path)
            stored_models[provider] = value
            _save_selected_models(storage_path, stored_models)


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
    tmp_path.write_text(json.dumps(models, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
