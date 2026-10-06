# F13 — Gatilhos por palavras

Executa uma função quando determinadas palavras aparecem na transcrição de uma gravação do mesmo autor. Ignora maiúsculas e pontuação, compara palavras inteiras e evita repetir o mesmo gatilho nessa gravação.

**Uso no código:** registar com `triggerSaidKeyworkd(callback, "palavra")`. O exemplo existente deteta «pijama» e escreve `pijama` num canal de texto disponível.

**Onde está:**

- [said_keywords.go](../../discord_bot/said_keywords.go): registo, correspondência, consulta e exemplo `sayPijama`.
- [main.go](../../discord_bot/main.go): registo inicial dos gatilhos.
- [said_keywords_test.go](../../discord_bot/said_keywords_test.go): exemplos e verificação da deteção.

**Contexto:** funciona com texto confirmado de Realtime ou com resultados Batch. Não usa IA; os registos e a deduplicação ficam em memória.
