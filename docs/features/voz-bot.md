# F05 — Voz do bot

Sintetiza respostas localmente com Piper e a voz portuguesa Tugão. Entrega áudio por segmentos e ajusta a expressão a partir de emojis. A música cede a saída durante a fala e retoma depois.

**Configuração:** `ASSISTANT_VOICE_ENABLED` liga/desliga a voz; `ASSISTANT_VOICE_MODEL` aponta para o modelo. Se a síntese falhar, a resposta mantém-se no chat.

**Onde está:**

- [speech.go](../../discord_bot/speech.go): preparação, reprodução e cancelamento da fala.
- [speech_synthesis.py](../../discord_bot/speech_synthesis.py): síntese, segmentação e expressão.
- [music.go](../../discord_bot/music.go): coordenação da saída com a música.
- [Dockerfile](../../discord_bot/Dockerfile): instalação do Piper e modelo de voz.

**Liga-se a:** [assistente de voz](assistente-voz.md) e [reações espontâneas](reacoes-espontaneas.md).
