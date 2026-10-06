# Índice de funcionalidades

Nomes de referência para conversar sobre o projeto. Cada MD explica o comportamento, como o usar e onde está implementado. [Contexto geral](../CONTEXTO.md).

| ID | Nome e documento | Entrada principal no código |
| --- | --- | --- |
| F01 | [Captura de chamadas](captura-chamadas.md) | `discord_bot/audio.go` |
| F02 | [Transcrição de gravações](transcricao-gravacoes.md) | `src/transcription-api/app/transcriber.py` |
| F03 | [Transcrição em tempo real](transcricao-realtime.md) | `discord_bot/streaming.go` |
| F04 | [Assistente de voz](assistente-voz.md) | `discord_bot/assistant.go` |
| F05 | [Voz do bot](voz-bot.md) | `discord_bot/speech.go` |
| F06 | [Memória do grupo](memoria-grupo.md) | `src/transcription-api/app/group_memory.py` |
| F07 | [Reações espontâneas](reacoes-espontaneas.md) | `discord_bot/group_memory.go` |
| F08 | [Histórico e resumo do chat](historico-chat.md) | `discord_bot/text_messages.go` |
| F09 | [Resumo de chamadas](resumo-chamadas.md) | `src/transcription-api/app/agent.py` |
| F10 | [Perfis e lore](perfis-lore.md) | `src/transcription-api/app/profile_updater.py` |
| F11 | [Oráculo e jogo de citações](oraculo-citacoes.md) | `discord_bot/main.go`: `oracleHook`, `guessHook` |
| F12 | [Música na chamada](musica.md) | `discord_bot/music.go` |
| F13 | [Gatilhos por palavras](gatilhos-palavras.md) | `discord_bot/said_keywords.go` |
| F14 | [Configuração da IA](configuracao-ia.md) | `src/transcription-api/app/llm.py` |
| F15 | [Estado, consumo e recuperação](estado-recuperacao.md) | `src/transcription-api/app/workers.py` |
| F16 | [Apagar dados pessoais](apagar-dados.md) | `discord_bot/main.go`: `forgetHook` |
| F17 | [Idioma do bot](idioma.md) | `discord_bot/bot_language.go` |

As funcionalidades estão presentes no código; algumas dependem de chaves, modelos ou configuração. Estes nomes organizam a documentação e não alteram os comandos existentes.
