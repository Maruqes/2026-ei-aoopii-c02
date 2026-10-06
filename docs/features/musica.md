# F12 — Música na chamada

Reproduz áudio de vídeos públicos do YouTube e mantém uma fila na chamada. Usa yt-dlp e FFmpeg; a gravação da conversa continua durante a reprodução.

**Uso:** `/play url:...`, `/pause`, `/skip`, `/queue` e `/musicstop`. Os controlos exigem estar no mesmo canal de voz do bot.

**Onde está:**

- [music.go](../../discord_bot/music.go): fila, reprodução e coordenação com a voz sintetizada.
- [music_commands.go](../../discord_bot/music_commands.go): comandos, mensagens e validações de acesso.
- [bot_language.go](../../discord_bot/bot_language.go): registo dos comandos.
- [Dockerfile](../../discord_bot/Dockerfile): ferramentas necessárias à reprodução.

**Contexto:** vídeos até uma hora e até 20 faixas na fila. A fila fica em memória e desaparece ao desligar/reiniciar o bot.
