"""Offline OpenCode Go provider configuration, wiring and catalog checks."""

from dataclasses import replace
from types import SimpleNamespace

import openai
import pytest
from app import main
from app.config import Settings
from app.llm import OPENCODE_SESSION, OpenCodeGoClient
from app.model_selection import configured_model, current_model, select_model


@pytest.fixture(autouse=True)
def clear_client_cache():
    main.build_llm_client.cache_clear()
    yield
    main.build_llm_client.cache_clear()


def settings(**overrides):
    return replace(
        Settings(
            database_url="unused",
            llm_provider="opencode-go",
            opencode_api_key="test-key",
            opencode_session_id="",
        ),
        **overrides,
    )


class FakeOpenAI:
    catalog = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeOpenAI.instances.append(self)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_completion)
        )
        self.models = SimpleNamespace(
            list=lambda: SimpleNamespace(data=FakeOpenAI.catalog)
        )

    def _create_completion(self, **kwargs):
        FakeOpenAI.completions.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="Ola!"),
                )
            ]
        )


@pytest.fixture
def fake_openai(monkeypatch):
    FakeOpenAI.instances = []
    FakeOpenAI.completions = []
    FakeOpenAI.catalog = []
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    return FakeOpenAI


def test_settings_read_opencode_environment(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "opencode-go")
    monkeypatch.setenv("OPENCODE_API_KEY", "env-key")
    monkeypatch.setenv(
        "OPENCODE_BASE_URL", "https://opencode.ai/inference/go/openai/v1/"
    )
    monkeypatch.setenv(
        "OPENCODE_MODELS_BASE_URL", "https://opencode.ai/inference/go/v1/"
    )
    monkeypatch.setenv("OPENCODE_MODEL", "glm-5.3")
    monkeypatch.setenv("OPENCODE_SESSION_ID", "session-1")
    loaded = Settings.from_env()
    assert loaded.llm_provider == "opencode-go"
    assert loaded.opencode_api_key == "env-key"
    assert loaded.opencode_base_url == "https://opencode.ai/inference/go/openai/v1"
    assert loaded.opencode_models_base_url == "https://opencode.ai/inference/go/v1"
    assert loaded.opencode_model == "glm-5.3"
    assert loaded.opencode_session_id == "session-1"


def test_opencode_model_selection_is_provider_scoped(tmp_path):
    loaded = settings(opencode_model="deepseek-v4.1-flash")
    assert configured_model(loaded) == "deepseek-v4.1-flash"
    path = tmp_path / "models.json"
    select_model("opencode-go", "glm-5.3", path)
    assert current_model(replace(loaded, llm_model_selection_file=path)) == "glm-5.3"


def test_build_llm_client_returns_opencode_client():
    client = main.build_llm_client(settings(), "deepseek-v4.1-flash")
    assert isinstance(client, OpenCodeGoClient)
    assert client.model == "deepseek-v4.1-flash"
    assert client.provider_name == "opencode-go"
    assert client.api_key_env == "OPENCODE_API_KEY"
    assert client.base_url == "https://opencode.ai/inference/go/openai/v1"
    assert client.models_base_url == "https://opencode.ai/inference/go/v1"


def test_chat_client_uses_opencode_base_url_and_headers(fake_openai):
    client = OpenCodeGoClient(
        api_key="test-key",
        base_url="https://opencode.ai/inference/go/openai/v1",
        models_base_url="https://opencode.ai/inference/go/v1",
        model="deepseek-v4.1-flash",
    )
    assert client.test_model() == "Ola!"
    created = fake_openai.instances[0].kwargs
    assert created["base_url"] == "https://opencode.ai/inference/go/openai/v1"
    headers = created["default_headers"]
    assert headers["x-opencode-session"] == OPENCODE_SESSION
    assert headers["User-Agent"] == "discord-anthropologist/1.0"
    request = fake_openai.completions[0]
    assert request["model"] == "deepseek-v4.1-flash"
    assert request["max_completion_tokens"] == 2500
    assert "response_format" not in request


def test_explicit_session_id_wins_over_process_default(fake_openai):
    client = OpenCodeGoClient(
        api_key="test-key",
        base_url="https://opencode.ai/inference/go/openai/v1",
        models_base_url="https://opencode.ai/inference/go/v1",
        model="deepseek-v4.1-flash",
        session_id="stable-session",
    )
    client.test_model()
    headers = fake_openai.instances[0].kwargs["default_headers"]
    assert headers["x-opencode-session"] == "stable-session"


def test_list_models_uses_catalog_base_url_and_deduplicates(fake_openai):
    fake_openai.catalog = [
        SimpleNamespace(id="beta"),
        SimpleNamespace(id="alpha"),
        SimpleNamespace(id=" alpha "),
        SimpleNamespace(id=""),
        SimpleNamespace(id=None),
        SimpleNamespace(),
    ]
    client = OpenCodeGoClient(
        api_key="test-key",
        base_url="https://opencode.ai/inference/go/openai/v1",
        models_base_url="https://opencode.ai/inference/go/v1",
        model="deepseek-v4.1-flash",
    )
    assert client.list_models() == ["alpha", "beta"]
    assert fake_openai.instances[0].kwargs["base_url"] == (
        "https://opencode.ai/inference/go/v1"
    )
    assert fake_openai.instances[0].kwargs["default_headers"]["x-opencode-session"]


def test_missing_key_names_the_environment_variable(fake_openai):
    client = OpenCodeGoClient(
        api_key=None,
        base_url="https://opencode.ai/inference/go/openai/v1",
        models_base_url="https://opencode.ai/inference/go/v1",
        model="deepseek-v4.1-flash",
    )
    with pytest.raises(RuntimeError, match="OPENCODE_API_KEY"):
        client.list_models()
    with pytest.raises(RuntimeError, match="OPENCODE_API_KEY"):
        client.test_model()
    assert fake_openai.instances == []


def test_structured_json_gets_a_larger_output_budget(fake_openai):
    from app.llm import STRUCTURED_OUTPUT_TOKENS

    client = OpenCodeGoClient(
        api_key="test-key",
        base_url="https://opencode.ai/inference/go/openai/v1",
        models_base_url="https://opencode.ai/inference/go/v1",
        model="deepseek-v4.1-flash",
    )
    client.test_model()
    assert fake_openai.completions[-1]["max_completion_tokens"] == 2500
    assert "reasoning_effort" not in fake_openai.completions[-1]

    client._chat(system="system", user="user", json_format=True)
    request = fake_openai.completions[-1]
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_completion_tokens"] == max(2500, STRUCTURED_OUTPUT_TOKENS)
    assert request["max_completion_tokens"] >= 4096
    # Reasoning tokens would otherwise consume the whole JSON budget.
    assert request["reasoning_effort"] == "none"
