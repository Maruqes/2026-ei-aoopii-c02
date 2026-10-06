# F17 — Idioma do bot

Alterna mensagens e descrições de comandos entre português e inglês, passando também o idioma aos pedidos de IA que o suportam.

**Uso:** `/language` altera o idioma; `BOT_LANGUAGE` define o valor inicial. O assistente de voz mantém respostas em PT-PT.

**Onde está:**

- [bot_language.go](../../discord_bot/bot_language.go): idioma atual, traduções e construção dos comandos.
- [main.go](../../discord_bot/main.go): `languageHook` e atualização dos comandos Discord.
- [llm.py](../../src/transcription-api/app/llm.py): instruções de idioma para resumos e respostas.

**Contexto:** o estado do idioma é global ao processo do bot, não uma preferência por servidor.
