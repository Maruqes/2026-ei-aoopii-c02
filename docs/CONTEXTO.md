# Contexto do projeto

O **Discord Anthropologist** acompanha conversas de voz e texto num servidor Discord. Transcreve chamadas, cria resumos, mantém perfis e lore dos membros, responde a perguntas e toca música.

## Onde está o quê

| Parte | Localização | Responsabilidade |
| --- | --- | --- |
| Bot Discord (Go) | [discord_bot](../discord_bot/) | Comandos, captura de áudio/texto, reprodução e entrega de respostas. |
| API (Python/FastAPI) | [src/transcription-api/app](../src/transcription-api/app/) | Transcrição, IA, memória e processamento em segundo plano. |
| Dados (PostgreSQL) | [src/data](../src/data/) | Histórico, sessões, perfis, preferências e filas persistentes. |
| Arranque | [docker-compose.yml](../docker-compose.yml), [Makefile](../Makefile) | API, bot e base de dados; `make compose` inicia os serviços. |
| Configuração | [.env.example](../.env.example) | Exemplo das variáveis de ambiente. |
| Pedidos de exemplo | [BrunoAPI](../BrunoAPI/) | Coleção para experimentar a API. |
| Planos e decisões | [plan](../plan/) | Histórico de desenho e verificações; consultar o código para o comportamento atual. |

## Fluxo principal

1. O bot recebe áudio ou mensagens no Discord.
2. A API transcreve o áudio e guarda as observações em PostgreSQL.
3. A IA usa esse histórico para resumos, perfis, memória e respostas.
4. O bot publica texto, voz ou reações no Discord.

A transcrição e a IA têm fornecedores separados: Whisper/Speechmatics/Deepgram para áudio; clientes compatíveis com OpenAI, Groq, Ollama ou ChatGPT para IA. Os ficheiros de recuperação e as filas permitem retomar trabalho após falhas.

## Como referir funcionalidades

Usar o **nome**, o **ID** ou o **ficheiro** do [índice de funcionalidades](features/README.md). Exemplo: «altera F04 — Assistente de voz» ou «vê `assistente-voz.md`».

Este mapa descreve o código e a documentação lidos em **06/10/2026**. Não confirma uma execução real no Discord. O [plano da conversa por voz](../plan/voice-conversation.md) ainda assinala ensaios reais com duas pessoas, ruído, música e medições de latência/consumo como pendentes.
