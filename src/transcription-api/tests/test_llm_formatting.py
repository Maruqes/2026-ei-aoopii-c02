import importlib.util
import sys
import types
from pathlib import Path


repository_module = types.ModuleType("data.repository")
repository_module.UserProfile = type("UserProfile", (), {})
data_module = types.ModuleType("data")
data_module.repository = repository_module
original_data_module = sys.modules.get("data")
original_repository_module = sys.modules.get("data.repository")
sys.modules["data"] = data_module
sys.modules["data.repository"] = repository_module

llm_path = Path(__file__).resolve().parents[1] / "app" / "llm.py"
spec = importlib.util.spec_from_file_location("llm_under_test", llm_path)
assert spec is not None and spec.loader is not None
llm = importlib.util.module_from_spec(spec)
sys.modules["llm_under_test"] = llm
try:
    spec.loader.exec_module(llm)
finally:
    if original_data_module is None:
        sys.modules.pop("data", None)
    else:
        sys.modules["data"] = original_data_module
    if original_repository_module is None:
        sys.modules.pop("data.repository", None)
    else:
        sys.modules["data.repository"] = original_repository_module

clean_answer = llm.clean_answer
guild_oracle_system = llm.guild_oracle_system
normalize_summary = llm.normalize_summary
profile_prompt_system = llm.profile_prompt_system
session_summary_system = llm.session_summary_system


def test_clean_answer_preserves_discord_bullets_and_full_text() -> None:
    content = "**Resposta curta**\n- " + ("x" * 2200) + "\n\n\n**Limites**\n- Nada a apontar."

    cleaned = clean_answer(content)

    assert "**Resposta curta**\n- " in cleaned
    assert "\n\n\n" not in cleaned
    assert cleaned.endswith("- Nada a apontar.")
    assert len(cleaned) > 2200


def test_normalize_summary_does_not_truncate() -> None:
    content = "**Resumo**\n- " + ("x" * 2200)

    assert normalize_summary(content).endswith("x" * 20)


def test_prompt_system_uses_standard_discord_sections() -> None:
    prompt = profile_prompt_system()

    assert "**Resposta curta**" in prompt
    assert "**Pontos da lore**" in prompt
    assert "**Roast contextual**" in prompt
    assert "Markdown tables" in prompt


def test_oracle_system_requires_server_and_relationship_view() -> None:
    prompt = guild_oracle_system()

    assert "**Visao geral**" in prompt
    assert "**Pessoas e relacoes**" in prompt
    assert "**Roast cruzado**" in prompt
    assert "Read the whole guild context" in prompt


def test_summary_system_requires_full_topic_timeline_and_roast() -> None:
    prompt = session_summary_system()

    assert "**Linha do tempo**" in prompt
    assert "**Cruzamentos e roast**" in prompt
    assert "every substantive topic" in prompt
