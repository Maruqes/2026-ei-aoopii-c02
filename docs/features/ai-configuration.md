# F14 — AI configuration

Defines the provider and model used for answers, summaries and profiles. It supports OpenAI/Groq-compatible clients, Ollama, OpenCode Go and ChatGPT account authentication. The choice is independent of the transcription provider.

**Usage:** `LLM_PROVIDER` in the environment; `/models` lists/tests/selects models and `/effort` adjusts reasoning effort. `make codex` starts the ChatGPT login.

**Where it lives:**

- [llm.py](../../src/transcription-api/app/llm.py): AI clients and prompts.
- [model_selection.py](../../src/transcription-api/app/model_selection.py): persistence of the model and effort choice.
- [chatgpt_llm.py](../../src/transcription-api/app/chatgpt_llm.py), [chatgpt_auth.py](../../src/transcription-api/app/chatgpt_auth.py) and [chatgpt_control.py](../../src/transcription-api/app/chatgpt_control.py): ChatGPT integration.
- [main.go](../../discord_bot/main.go): `modelsHook` and `effortHook`.
- [CHATGPT_SETUP.md](../../CHATGPT_SETUP.md): setup and login instructions.
- [OPENCODE.md](../../OPENCODE.md): OpenCode Go provider setup and diagnostics.

**Context:** available capabilities and models depend on the configured provider.
