# F14 — Configuração da IA

Define o fornecedor e o modelo usados por respostas, resumos e perfis. Suporta clientes compatíveis com OpenAI/Groq, Ollama e autenticação com conta ChatGPT. A escolha é independente do fornecedor de transcrição.

**Uso:** `LLM_PROVIDER` no ambiente; `/models` lista/testa/seleciona modelos e `/effort` ajusta o esforço de raciocínio. `make codex` inicia o login ChatGPT.

**Onde está:**

- [llm.py](../../src/transcription-api/app/llm.py): clientes de IA e prompts.
- [model_selection.py](../../src/transcription-api/app/model_selection.py): persistência da escolha de modelo e esforço.
- [chatgpt_llm.py](../../src/transcription-api/app/chatgpt_llm.py), [chatgpt_auth.py](../../src/transcription-api/app/chatgpt_auth.py) e [chatgpt_control.py](../../src/transcription-api/app/chatgpt_control.py): integração ChatGPT.
- [main.go](../../discord_bot/main.go): `modelsHook` e `effortHook`.
- [CHATGPT_SETUP.md](../../CHATGPT_SETUP.md): instruções de configuração e login.

**Contexto:** capacidades e modelos disponíveis dependem do fornecedor configurado.
