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
para humanos com reserva na chamada. Diz «Olá macaco» para ouvir «Diz» e receber
a confirmação no chat. Faz a pergunta e continua a conversar sem repetir a ativação;
também aceita «Olá macaco, explica polimorfismo…». «Adeus macaco» ou «obrigado,
adeus macaco» termina com uma despedida curta. O assistente responde em PT-PT,
com 2–4 frases por defeito e mais detalhe a pedido, usando os últimos cinco minutos
da chamada atual e até 12 trocas recentes do diálogo. Cada conversa fica guardada e atualiza o perfil e
a lore de quem falou em segundo plano, sem esperar pelo fim da chamada. As respostas
do bot são contexto identificado, nunca factos sobre a pessoa. Não executa comandos.

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

A memória também acompanha a conversa sem a frase de ativação: a cada bloco fechado
de `GROUP_MEMORY_BULK_MINUTES` (5 por defeito), guarda temas, decisões e lore, e atualiza
os perfis dos participantes. `GROUP_MEMORY_CONTEXT_BULKS=3` dá às reações os últimos
3 blocos (aceita 1–3); o histórico guardado continua pesquisável em `/oracle`.
O bot pode comentar ou fazer uma piada por texto e, numa pausa da chamada, por voz.
Espera pelo assistente, pela música e pela fala; uma nova fala cancela a voz espontânea.
Sem cobertura Realtime completa, publica apenas texto. Não obriga a fazer uma piada
por bloco e respeita `GROUP_MEMORY_REACTION_COOLDOWN_MINUTES=10`.

GIFs usam ações, cenas e analogias visuais ligadas à conversa, apenas quando a piada
surge naturalmente. A pesquisa GIPHY com `GIPHY_API_KEY` varia entre resultados e evita
os últimos 20 GIFs escolhidos por servidor enquanto o bot está ligado; sem resultados
novos, publica apenas texto. A integração não acede aos favoritos ou ao seletor privado
de GIFs do Discord; sem chave, continua com texto/voz. `ASSISTANT_VOICE_ENABLED=false`
desliga voz. `GROUP_MEMORY_REACTIONS_ENABLED=false` desliga as intervenções mantendo
memória e perfis; `/assistant disable` silencia as reações desse servidor.

Alterações exigem **Gerir Servidor**, são persistidas e cancelam pedidos pendentes.
O destino inicial é o canal de resumo da chamada; sem acesso a um destino válido,
o assistente não inicia pedidos. Só o autor da ativação fornece a pergunta. Há uma
conversa de cada vez; outras ativações recebem um aviso de ocupado, sem fila.
Toda a fala do autor durante a conversa é dirigida ao bot; as outras vozes servem
como contexto. Nova fala reconhecida do autor interrompe geração ou voz, preserva
uma pergunta ainda sem resposta entregue e inicia o próximo turno. Se a resposta
já estava no chat, o histórico identifica a voz interrompida. «Cancela» como enunciado
isolado encerra a conversa; sair da chamada também cancela imediatamente.

A pergunta junta os segmentos do mesmo autor e termina após um período contínuo
sem som desse autor. Define `ASSISTANT_SILENCE_SECONDS=5` no `.env` para esperar
5 segundos (default; aceita 0,5–20 segundos, com ponto nos valores decimais).
Voltar a falar reinicia o contador; outras vozes e finais atrasados não o reiniciam.
O contador usa o áudio recebido e os tempos de fala reconhecida, para incluir fala
baixa. Depois do silêncio, ainda aguarda os finais correspondentes ao áudio.
Só texto final
entra no pedido à IA; números, datas e pontuação são preservados.
`ASSISTANT_INACTIVITY_SECONDS=30` dá 30 segundos disponíveis para responder e encerra
silenciosamente a conversa se não houver fala. Geração e voz suspendem esse relógio;
o fim de uma resposta ou aviso recuperável abre uma janela nova.
`ASSISTANT_CAPTURE_SECONDS=60` limita cada turno a 60 segundos; ambos os valores
aceitam 5–300 segundos. O limite de 2000 caracteres também se mantém: não se envia
uma pergunta cortada. O LLM conserva o prazo independente de 30 segundos.
Finais em falta, falhas de streaming ou do LLM produzem um aviso curto e mantêm a
conversa para repetir sem ativação. Batch continua a servir as gravações, mas nunca
ativa nem alimenta turnos do assistente.
Tempos sobrepostos nos finais não cancelam a captura; palavras já recebidas e
marcadores vazios não repetem a pergunta. Tempos inválidos continuam a ser rejeitados.
Os limites encerram apenas o turno: a mesma pessoa pode continuar a conversa.
Uma mudança normal de gravação/SSRC mantém a pergunta e aguarda os finais da gravação
anterior, ordenados pelos tempos de áudio. Silêncio ou finais vazios após a ativação
não antecipam o prazo para começar a pergunta. O Speechmatics usa finais com
`max_delay=2` e modo flexível, preservando a formatação de números e datas.
A voz é sintetizada por segmentos; a confirmação e a resposta no chat são publicadas
quando o primeiro áudio está pronto para tocar. Se a síntese falhar, mantém a
resposta por texto. Uma pergunta já incluída na ativação dispensa o «Diz» por voz.
A confirmação só ocorre na abertura; o histórico ativo fica em memória e é limpo após encerramento
ou reinício. O prompt completo respeita `LLM_CONTEXT_CHARS`: prioriza o diálogo
recente e identifica cobertura parcial quando falta espaço, sem resumir cada turno
através de outra chamada ao LLM.

O limite de reservas Realtime existente mantém-se. `/assistant status` e avisos de
cobertura identificam quem está sem reserva Realtime. Os avisos aguardam a primeira
sincronização e 15 segundos de estado estável, com no máximo um aviso por minuto.
Não se garante
cobertura para todos sem confirmar a capacidade da conta. Silêncio enviado para
fechar enunciados também consome minutos Realtime; consulta `/keys` e os logs
`assistant activation`, `assistant turn closed`, `assistant delivery` e
`assistant response` para medir abertura, fecho, primeira entrega e fim; `/keys`
mostra consumo. `voice_ready=true` identifica entrega no início da voz.

A deteção de fala usa inicialmente energia PCM (`ASSISTANT_SPEECH_RMS=500`); deve ser validada com microfones,
ruído e música reais. O ensaio Discord, capacidade e metas de latência ainda estão
pendentes.

O assistente também lê a resposta na chamada com uma voz local em português de
Portugal com [Piper](https://github.com/OHF-Voice/piper1-gpl) e a voz neural Tugão,
incluídos no Docker. A fala usa fonemas 30% mais longos, sem alterar o tom, e pausas
de 400 ms entre frases. Não precisa de outra key nem de rede durante a síntese.
A música cede a saída durante a fala e retoma no mesmo
ponto; uma pausa manual mantém-se. A resposta completa fica no chat mesmo que
a síntese falhe. Para respostas longas, fala até 2000 caracteres e indica o chat
para o restante. Desligar o assistente, mudar a configuração ou sair da chamada
cancela a fala em curso.

Os emojis da resposta dão expressão à frase imediatamente anterior (ou à primeira
frase, quando aparecem no início): 😊/😄/🎉 tornam a voz mais alegre, 😂/🤣/😆
acrescentam um riso curto, 😢/😔/💔 suavizam e abrandam a voz, e 😭 acrescenta uma
ligeira tremulação. 😮/😱/🤯 indicam surpresa, 😡/😠 irritação, ❤️/🥰/🙏 carinho,
😉/😜/😏 brincadeira, 🤔 reflexão e 😴 cansaço. Os símbolos e variantes de cor de
pele não são lidos; emojis repetidos não repetem o riso. O texto no chat mantém-se.
São aproximações por tom, ritmo, volume e vocalizações sintetizadas: Tugão não é um
modelo de interpretação emocional nem produz choro ou gargalhadas naturais.
O modelo carrega uma única vez por resposta; após 12 mudanças de expressão, o
restante texto é lido com a voz normal.

`ASSISTANT_VOICE_ENABLED=true` é o default. Usa `false` para resposta apenas no chat.
Depois de atualizar, reconstrói API e bot com
`docker compose up -d --build api discord-bot`.
Para execução fora do Docker, instala `piper-tts==1.8.0` e descarrega o
[modelo Tugão](https://huggingface.co/rhasspy/piper-voices/tree/v1.0.0/pt/pt_PT/tug%C3%A3o/medium)
e a configuração `.onnx.json` para o mesmo diretório. Coloca `piper` no `PATH` e
define `ASSISTANT_VOICE_MODEL` com o caminho do `.onnx` (22 050 Hz, mono).
O [plano da conversa contínua](plan/voice-conversation.md) mantém o ensaio Discord,
as medições de primeira voz/consumo e a avaliação com duas pessoas, ruído e música
pendentes.


## Deepgram and Speechmatics

Set `DEEPGRAM_API_KEY` (or numbered `DEEPGRAM_API_KEY_01`, `_02`, …) in `.env`, then
set `TRANSCRIPTION_PROVIDER_ORDER=deepgram,speechmatics`. Rebuild/restart the API and
bot with `make up`. Migration `009_transcription_providers.sql` is applied on API
startup. Secrets and project associations are read only by the Python API.

`/stt status` shows the effective order; `/stt order providers:speechmatics,deepgram`
switches it without restarting. The four choices include either provider alone.
Preferences persist per server, override the environment, and apply to new WAV units.
A healthy stream finishes its current unit before changing provider. `/streaming`
controls streaming independently, and `/keys` shows both providers even when one is
only a fallback. With an empty/absent order, the legacy `TRANSCRIPTION_PROVIDER`
continues to select Whisper or one remote provider.

Deepgram uses Nova-3, `pt-PT`, mono 48 kHz streaming, and mono WAV uploads derived
from the original safety recording. `DEEPGRAM_KEYTERMS` supplies comma-separated
terms to both modes. REST and WebSocket URLs must use the same host/region; for EU
use `https://api.eu.deepgram.com/v1` and `wss://api.eu.deepgram.com/v1/listen`.

Deepgram limits are shared per project, not multiplied per key. Unknown associations
share one group and also observe an aggregate ceiling. Configure `DEEPGRAM_PROJECT_ID`
and optional suffix overrides (`DEEPGRAM_PROJECT_ID_02`,
`DEEPGRAM_STREAMING_LIMIT_02`, `DEEPGRAM_BATCH_LIMIT_02`) only for actual account
associations/quotas. The default local ceilings are 150 streams and 50 WAV requests.
When management permissions identify a project, counters merge and the smallest
configured group ceiling wins. Speechmatics retains its local two-streams-per-key
configuration. Admission counters require one API process.

Capacity errors cool down the affected product/group for ten seconds; invalid
credentials affect the key/mode, and confirmed credit failures affect the billing
group. Fallback works in either order. If all streams are occupied, capture continues
as WAV and participants wait FIFO. A failed stream immediately ends its WAV unit,
recovers it through the durable queue, and gets a fresh assignment. Batch recovery
replaces the unit's text atomically and never sends old speech to the assistant.
Only confirmed exhaustion of every allowed configured provider, without pending
remote work/results, permits the existing credit-discard policy. Authentication and
temporary failures preserve pending audio; temporary failures retry after 15 seconds.
`/streaming on` explicitly revalidates locally rejected credentials after correction.

Deepgram successful REST results and request IDs are atomically saved in
`<recording>.deepgram.json` before final DB persistence; they can be reused after
restart. Speechmatics job IDs/old sidecars retain their original key identity.
A lost HTTP response can require a second billed request. `/forget`, normal cleanup,
and credit cleanup include both providers' sidecars and derived WAVs.

`/keys` shows credits, streams in use against the configured local limit, key health,
and estimated costs in a compact summary. Deepgram balances require `billing:read`;
a 403 is shown as missing permission, and unknown balances never look like zero.
Management failures never disable transcription. Detailed local audio hours, models,
provider usage and pricing remain in `GET /v1/transcription/keys`. Shared balances
and remote Speechmatics usage must not be added per key. Cost estimates exclude
grants, discounts and taxes. `/health` and `/streaming status` identify active providers.

Validation uses local REST/WebSocket simulators and a disposable PostgreSQL database:
`TEST_DATABASE_URL=... pytest` and `cd discord_bot && go test -race ./...`.
Before choosing a provider on recognition quality, compare the same real PT-PT audio
against a human reference; the simulators validate protocol/recovery, not accuracy.
Rollback: choose Speechmatics alone with `/stt order`, or disable streaming. Complete
Deepgram units before downgrading the binary; leave the additive migration in place.
