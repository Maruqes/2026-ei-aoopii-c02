# F03 — Transcrição em tempo real

Envia áudio durante a chamada por WebSocket para Speechmatics ou Deepgram. Gere reservas por participante e uma fila por ordem de entrada; quem não tem vaga continua com gravações Batch.

**Uso:** `/streaming mode:on`, `off` ou `status`; alterações exigem Gerir Servidor. A preferência fica guardada por servidor e começa desligada por defeito.

**Onde está:**

- [streaming.go](../../discord_bot/streaming.go): participantes, envio de áudio e eventos reconhecidos.
- [streaming_routes.py](../../src/transcription-api/app/streaming_routes.py): preferência, reservas e `/v1/streaming/audio`.
- [realtime.py](../../src/transcription-api/app/realtime.py): pool de reservas e ponte para os fornecedores.
- [providers.py](../../src/transcription-api/app/providers.py): capacidade e saúde dos fornecedores.

**Contexto:** falhas podem recuperar pelo WAV sem repetir texto. O pool requer um único processo da API. Alimenta o [assistente de voz](assistente-voz.md).
