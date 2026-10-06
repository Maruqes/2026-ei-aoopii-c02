# F04 — Assistente de voz

«Olá macaco» abre uma conversa com o bot. Depois da ativação, a mesma pessoa pode continuar sem repetir a frase. O bot espera pelo silêncio, responde em PT-PT no chat e pode ler a resposta na chamada. Usa os últimos cinco minutos da chamada e o diálogo recente; «Adeus macaco» ou «Cancela» encerra a conversa.

**Uso:** `/assistant status`, `enable`, `disable`, `phrase` e `channel`. A configuração é por servidor; alterações exigem Gerir Servidor.

**Onde está:**

- [assistant.go](../../discord_bot/assistant.go): ativação, captura da pergunta, silêncio, interrupções e entrega.
- [assistant_routes.py](../../src/transcription-api/app/assistant_routes.py): configurações e `POST /v1/assistant/question`.
- [llm.py](../../src/transcription-api/app/llm.py): `answer_question` e construção do contexto da IA.
- [voice-conversation.md](../../plan/voice-conversation.md): decisões e ensaios pendentes.

**Contexto:** exige [Realtime](transcricao-realtime.md) para o autor; há uma conversa ativa de cada vez. Novas falas podem interromper a geração ou a voz. Tempos são configurados por `ASSISTANT_*`.
