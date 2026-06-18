from dataclasses import replace
from pathlib import Path

from app.config import Settings

from app.model_selection import configured_model, current_model, select_model


def test_selected_model_overrides_configured_model() -> None:
    settings = Settings(database_url="postgresql://test", openai_model="configured-model")
    provider = "test-openai-selection"
    settings = replace(settings, llm_provider=provider)

    assert configured_model(settings) == "configured-model"
    assert current_model(settings) == "configured-model"

    select_model(provider, "selected-model")

    assert current_model(settings) == "selected-model"


def test_selection_is_scoped_by_provider() -> None:
    openai = Settings(database_url="postgresql://test", llm_provider="openai-test", openai_model="openai-default")
    groq = Settings(database_url="postgresql://test", llm_provider="groq", groq_model="groq-default")

    select_model(openai.llm_provider, "openai-selected")

    assert current_model(openai) == "openai-selected"
    assert current_model(groq) == "groq-default"


def test_persisted_model_overrides_configured_model(tmp_path: Path) -> None:
    storage_path = tmp_path / "models.json"
    settings = Settings(
        database_url="postgresql://test",
        llm_provider="persisted-openai",
        openai_model="env-model",
        llm_model_selection_file=storage_path,
    )

    select_model(settings.llm_provider, "persisted-model", storage_path)

    restarted_settings = Settings(
        database_url="postgresql://test",
        llm_provider=settings.llm_provider,
        openai_model="changed-env-model",
        llm_model_selection_file=storage_path,
    )
    assert current_model(restarted_settings) == "persisted-model"
