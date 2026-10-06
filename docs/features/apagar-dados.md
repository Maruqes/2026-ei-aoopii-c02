# F16 — Apagar dados pessoais

Apaga dados guardados de um membro, incluindo mensagens, perfil, documento de lore e áudio local associado. Coordena a captura e os envios pendentes para executar a remoção.

**Uso:** `/forget` para os próprios dados; apagar os de outra pessoa exige Gerir Servidor.

**Onde está:**

- [main.go](../../discord_bot/main.go): `forgetHook` e verificação de permissões.
- [audio.go](../../discord_bot/audio.go): `forgetUserWithLocalAudio`, pausa/fecho da captura e remoção dos ficheiros locais.
- [main.py](../../src/transcription-api/app/main.py): `DELETE /v1/users/{discord_id}`.
- [repository.py](../../src/data/repository.py): remoção dos dados persistidos.
- [docs_client.py](../../src/transcription-api/app/docs_client.py) e [recording_cleanup.py](../../src/transcription-api/app/recording_cleanup.py): remoção de lore e ficheiros de áudio/recuperação.

**Contexto:** é uma operação de apagamento, não uma preferência permanente para deixar de recolher futuras mensagens.
