# F10 — Perfis e lore

Constrói perfis vivos dos membros a partir de observações de texto e voz, com traços e acontecimentos marcantes. Mantém também documentos Markdown locais com a lore.

**Uso:** `/profile` mostra o perfil; `/prompt` faz uma pergunta sobre a pessoa; `/sync` força a sincronização das observações de texto.

**Onde está:**

- [profile_updater.py](../../src/transcription-api/app/profile_updater.py): atualização dos perfis a partir das observações.
- [agent.py](../../src/transcription-api/app/agent.py): atualização a partir das sessões de voz.
- [docs_client.py](../../src/transcription-api/app/docs_client.py): leitura/escrita dos documentos de lore.
- [llm.py](../../src/transcription-api/app/llm.py): geração do perfil e respostas sobre a pessoa.
- [main.go](../../discord_bot/main.go): `profileHook`, `promptHook` e `syncHook`.

**Contexto:** recebe dados do chat, chamadas, assistente e memória do grupo. `LOCAL_PROFILE_DIR` define a pasta dos documentos; PostgreSQL guarda os perfis e referências.
