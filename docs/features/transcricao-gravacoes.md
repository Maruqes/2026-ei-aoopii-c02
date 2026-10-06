# F02 — Transcrição de gravações

Converte WAV em texto usando Whisper local, Speechmatics ou Deepgram. Os pedidos são processados em segundo plano e os resultados ficam associados à sessão e ao autor em PostgreSQL.

**Configuração:** `TRANSCRIPTION_PROVIDER` escolhe o fornecedor base. `TRANSCRIPTION_PROVIDER_ORDER` e `/stt order` definem a preferência e o fallback entre Deepgram e Speechmatics; `/stt status` mostra o estado.

**Onde está:**

- [transcriber.py](../../src/transcription-api/app/transcriber.py): Whisper e Speechmatics.
- [deepgram.py](../../src/transcription-api/app/deepgram.py): integração Deepgram.
- [transcription_router.py](../../src/transcription-api/app/transcription_router.py) e [providers.py](../../src/transcription-api/app/providers.py): escolha, limites e fallback.
- [main.py](../../src/transcription-api/app/main.py): `POST /v1/transcriptions` e `process_recording_file`.
- [transcription_commands.go](../../discord_bot/transcription_commands.go): comandos `/stt`.

**Contexto:** o WAV também permite recuperar falhas do [Realtime](transcricao-realtime.md). Áudio concluído é limpo após persistir o texto.
