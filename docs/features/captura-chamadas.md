# F01 — Captura de chamadas

O bot acompanha entradas/saídas de canais de voz, entra automaticamente quando aplicável e grava áudio em pequenos WAV por membro. Identifica quem falou, abre uma sessão e fecha-a para produzir o resumo.

**Uso:** `/start`, `/stop` e `/timeout` controlam o funcionamento e o tempo de saída.

**Onde está:**

- [audio.go](../../discord_bot/audio.go): ligação à chamada, participantes, gravações e fecho da sessão.
- [audio_convert.go](../../discord_bot/audio_convert.go) e [audio_buffer.go](../../discord_bot/audio_buffer.go): conversão e organização do áudio recebido.
- [ssrc_user_map.go](../../discord_bot/ssrc_user_map.go): associação entre fluxos de áudio e utilizadores.
- [main.go](../../discord_bot/main.go): `startHook`, `stopHook`, `timeoutHook` e recuperação das ligações no arranque.

**Liga-se a:** [transcrição de gravações](transcricao-gravacoes.md), [Realtime](transcricao-realtime.md) e [resumo de chamadas](resumo-chamadas.md).
