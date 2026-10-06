# F08 — Histórico e resumo do chat

Guarda mensagens de texto dos membros para contexto, perfis e consulta. Ignora mensagens privadas, bots, webhooks e mensagens sem texto. Pode resumir decisões, destaques e lore de um canal.

**Uso:** `/digest hours:24` resume o canal atual; aceita 1–168 horas e considera até 2000 mensagens recentes.

**Onde está:**

- [text_messages.go](../../discord_bot/text_messages.go): receção, filtros e identificação do autor/canal.
- [transcription_client.go](../../discord_bot/transcription_client.go): envio das mensagens à API.
- [main.go](../../discord_bot/main.go): `digestHook`.
- [main.py](../../src/transcription-api/app/main.py): `POST /v1/messages` e `/v1/guilds/{guild_id}/digest`.
- [repository.py](../../src/data/repository.py): persistência e consulta do histórico.

**Liga-se a:** [perfis e lore](perfis-lore.md) e [oráculo](oraculo-citacoes.md).
