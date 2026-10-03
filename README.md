# Discord Anthropologist

- 31415 Gonçalo Marques
- 31394 Ricardo Fernandes

## Run

Copy `.env.example` to `.env`, configure Discord and the chosen transcription/LLM provider,
then run `make compose`. Database migrations apply automatically when the API starts.
For NVIDIA Whisper, set `WHISPER_DEVICE=cuda` before using the Makefile.

To use a ChatGPT account for commands, summaries and profiles, set
`LLM_PROVIDER=chatgpt` and follow [ChatGPT setup](CHATGPT_SETUP.md).
Run `make codex` to open the official ChatGPT login in your browser. Discord
`/models` lists, tests and selects models available to that account; `/effort`
changes reasoning effort. Speech-to-text keeps its own provider.

The bot records voice in short per-member slices, stores text/voice observations in
Postgres, builds funny but grounded recaps, and maintains living member profiles.
Use `/digest` to catch up on a text channel, `/recap` for calls, `/oracle` to search the
group's history, `/profile` and `/prompt` for lore, and `/guess` for quote trivia.
`/health`, `/keys` and `/retry` expose progress, consumption and recovery.

Use `/play url:<YouTube video link>` to play audio in your call, `/pause` to pause or
resume, `/skip` to advance, `/queue` to inspect the queue and `/musicstop` to clear it
without stopping conversation recording. Playback accepts public videos up to one
hour, with at most 20 queued tracks. Docker includes yt-dlp, FFmpeg and Node.js.
Playback controls require sharing the bot's voice channel; the bot needs Connect/Speak permissions.
Queues are kept in memory and cleared when the bot disconnects or restarts.

Completed recordings and their Speechmatics recovery files are deleted once the
transcript is committed to Postgres. A startup/periodic sweep retries leftover cleanup.
Pending/failed recordings remain available for `/retry`; `/forget` also removes the
member's audio. YouTube audio is streamed through pipes, with temporary workspaces
removed after success, failure or cancellation.

Accepted recordings and profile updates survive API restarts. Local outbox files replay
failed submissions after a bot restart. Long conversations are summarized in ordered
slices; partial transcripts are identified in their recaps.

Speechmatics percentages compare measured Batch hours with an optional configured
hours budget. They do not represent account credit balance; several keys can share
account usage. See configuration, recovery details and test commands in the
[service documentation](src/transcription-api/README.md).

## Layout

- `discord_bot/`: Go Discord gateway, audio capture, commands and submission outbox.
- `src/transcription-api/`: FastAPI transcription service, LLM prompts, worker queues and profiles.
- `src/data/`: Postgres migrations, persistence and guild context retrieval.
- `docker-compose.yml`: local Postgres, transcription API and Discord bot.
- `BrunoAPI/`: local API request examples.

## Triggers por palavras na call

Em `discord_bot/main.go`, registar os triggers no arranque:

```go
triggerSaidKeyworkd(sayPijama, "pijama")
triggerSaidKeyworkd(funcDarAudio, "bot", "audio")
// Também aceita uma lista: triggerSaidKeyworkd(funcDarAudio, []string{"bot", "audio"}...)
```

A função recebe `SaidKeywordContext` e devolve `error`; o contexto inclui a sessão
Discord, servidor, canal de texto, autor e texto reconhecido. Todas as palavras têm
de aparecer na mesma gravação do mesmo autor, em qualquer ordem. Ignora maiúsculas
e pontuação, compara palavras inteiras e executa cada trigger uma vez por gravação.
Um erro da função permite nova tentativa na próxima consulta.

O exemplo “pijama” já está registado: envia `pijama` para o canal de sistema do
servidor, ou o primeiro canal de texto onde o bot possa escrever. As transcrições
são consultadas a cada segundo: Realtime reage ao texto confirmado; Batch reage
quando a transcrição da gravação fica disponível. Não precisa de LLM nem de
dependências adicionais. Os registos e a deduplicação ficam em memória.

## Transcrição Realtime (opcional)

Com `TRANSCRIPTION_PROVIDER=speechmatics` e keys configuradas, `/streaming mode:on`
ativa Realtime na chamada atual e guarda a preferência do servidor em Postgres.
`/streaming mode:off` volta a gravações Batch; `mode:status` mostra modo, vagas e fila.
O comando exige **Gerir Servidor** e responde de forma efémera. O valor inicial
`TRANSCRIPTION_STREAMING_ENABLED=false` aplica-se apenas a servidores sem preferência.

Cada segredo de key distinto dá duas reservas; utilizadores humanos são admitidos por
ordem de entrada e mantêm a reserva em silêncio. O excedente usa Batch até à promoção.
Realtime usa `enhanced/pt`, independentemente dos defaults Batch `melia-1/multi`.
O WAV temporário permanece até à confirmação durável; falhas recuperáveis usam Batch,
substituindo apenas o texto provisório daquele WAV. Créditos esgotados em todas as keys
suspendem a transcrição da chamada, eliminam áudio pendente e preservam texto confirmado.

Executar **um único processo da API** para este pool de reservas. Streaming vem desligado
por defeito; não é necessário mudar o comportamento de autojoin. Para detalhes de operação,
recuperação, debug e testes, consultar [a documentação da API](src/transcription-api/README.md#realtime).

## Assistente por voz: perguntas e respostas no chat

Com Realtime ativo (`/streaming mode:on`), o assistente fica disponível por defeito
para humanos com reserva na chamada. Diz «Olá macaco» para receber «Diz» no chat e
faz a pergunta; também aceita «Olá macaco, explica polimorfismo…». Depois de responder,
a próxima pergunta exige novamente a frase. O assistente geral responde em português,
sem consultar o histórico do servidor ou executar comandos.

- `/assistant status`: estado, frase, destino e participantes com/sem Realtime.
- `/assistant phrase value:"Olá macaco"`: frase com 2–5 palavras, até 50 caracteres.
- `/assistant channel value:#bot`: destino das confirmações e respostas.
- `/assistant enable` e `/assistant disable`: ligar/desligar por servidor.

A frase é procurada no início, meio ou fim do enunciado, ignorando maiúsculas,
acentos e pontuação. Aceita um pequeno erro de reconhecimento, até duas palavras
intercaladas e palavras juntas/separadas: «olha macaco», «olá meu macaco» e
«olamacaco» também ativam. A pergunta antes e depois da ativação é preservada,
mesmo dividida entre finais; uma pausa superior ao tempo de silêncio configurado
separa enunciados.
`/assistant phrase value:"outra frase"` altera a frase usada pelo servidor.

Alterações exigem **Gerir Servidor**, são persistidas e cancelam pedidos pendentes.
O destino inicial é o canal de resumo da chamada; sem acesso a um destino válido,
o assistente não inicia pedidos. Só o autor da ativação fornece a pergunta. Há uma
interação de cada vez; outras ativações recebem um aviso de ocupado, sem fila.
«Cancela» como enunciado isolado cancela a captura; sair da chamada também cancela.

A pergunta junta os segmentos do mesmo autor e termina após um período contínuo
sem som desse autor. Define `ASSISTANT_SILENCE_SECONDS=5` no `.env` para esperar
5 segundos (default; aceita 0,5–20 segundos, com ponto nos valores decimais).
Voltar a falar reinicia o contador; outras vozes e finais atrasados não o reiniciam.
O contador usa o áudio recebido e os tempos de fala reconhecida, para incluir fala
baixa. Depois do silêncio, ainda aguarda os finais correspondentes ao áudio.
Só texto final
entra no pedido à IA; números, datas e pontuação são preservados. Há 10 segundos
para começar e 30 segundos de captura a partir do reconhecimento da ativação,
e 30 segundos para o LLM. Finais em falta ou falhas de streaming cancelam o pedido;
Batch continua a servir as gravações, mas nunca ativa o assistente.
Tempos sobrepostos nos finais não cancelam a captura; palavras já recebidas e
marcadores vazios não repetem a pergunta. Tempos inválidos continuam a ser rejeitados.
Os limites encerram apenas o pedido: a escuta volta a ficar disponível na chamada.

O limite de reservas Realtime existente mantém-se. `/assistant status` e avisos de
cobertura identificam quem está sem reserva Realtime. Os avisos aguardam a primeira
sincronização e 15 segundos de estado estável, com no máximo um aviso por minuto.
Não se garante
cobertura para todos sem confirmar a capacidade da conta. Silêncio enviado para
fechar enunciados também consome minutos Realtime; consulta `/keys` e os logs
`assistant activation`/`assistant response` para medir consumo e latência.

A deteção de fala usa inicialmente energia PCM (`ASSISTANT_SPEECH_RMS=500`); deve ser validada com microfones,
ruído e música reais. O ensaio Discord, capacidade e metas de latência ainda estão
pendentes. Confirmações audíveis e TTS ficam para a etapa seguinte do
[plano](plan/hey-bot.md).
