# F15 — Estado, consumo e recuperação

Mostra a saúde dos serviços, ocupação e consumo dos fornecedores. Mantém filas persistentes na API e ficheiros de envio pendente no bot para retomar trabalho após falhas/reinícios; elimina ficheiros de gravações concluídas.

**Uso:** `/ping`, `/health`, `/keys` e `/retry session:...`.

**Onde está:**

- [main.go](../../discord_bot/main.go): comandos de diagnóstico e recuperação.
- [transcription_client.go](../../discord_bot/transcription_client.go): envio, tentativas e outbox local.
- [workers.py](../../src/transcription-api/app/workers.py): processamento dos trabalhos persistentes.
- [recording_cleanup.py](../../src/transcription-api/app/recording_cleanup.py): limpeza e recuperação de WAV.
- [provider_routes.py](../../src/transcription-api/app/provider_routes.py), [speechmatics_usage.py](../../src/transcription-api/app/speechmatics_usage.py) e [speechmatics_usage_alerts.go](../../discord_bot/speechmatics_usage_alerts.go): métricas e avisos.

**Contexto:** consumo local, consumo reportado e saldo são medidas diferentes. Falhas recuperáveis preservam áudio; esgotamento confirmado de todos os fornecedores permitidos pode suspender a captura e descartar o áudio pendente afetado.
