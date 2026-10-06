# F09 — Resumo de chamadas

Ao fechar a sessão, a API usa as transcrições para criar um resumo e atualizar perfis. Conversas longas são tratadas em partes ordenadas; os resumos identificam cobertura parcial quando falta transcrição.

**Uso:** o resumo é publicado no fim da chamada; `/recap` consulta uma sessão e permite ver resumo ou transcrição.

**Onde está:**

- [audio.go](../../discord_bot/audio.go): `finishSessionAndPostSummary`.
- [main.go](../../discord_bot/main.go): `recapHook`.
- [agent.py](../../src/transcription-api/app/agent.py): processamento da sessão e atualização dos participantes.
- [llm.py](../../src/transcription-api/app/llm.py): `summarize_session` e prompts dos resumos.
- [main.py](../../src/transcription-api/app/main.py): fecho de sessões, consulta e `build_session_recap`.

**Contexto:** depende da [captura](captura-chamadas.md) e da [transcrição](transcricao-gravacoes.md); sessões com falhas podem ser recuperadas com `/retry`.
