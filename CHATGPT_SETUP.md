# Usar uma conta ChatGPT no Discord Anthropologist

Esta opção usa a conta ChatGPT para gerar resumos, atualizar perfis e responder
aos comandos de IA, incluindo `/oracle`, `/digest` e `/prompt`. A transcrição
continua a usar o fornecedor configurado em `TRANSCRIPTION_PROVIDER`.

O fornecedor novo é `LLM_PROVIDER=chatgpt`. Os fornecedores `openai`, `groq` e
`ollama` continuam disponíveis. Não é necessária uma `OPENAI_API_KEY` para esta
opção; o login usa a integração oficial **Sign in with ChatGPT**.

## 1. Requisitos

- Docker com Compose e acesso HTTPS, a partir dos containers, a
  `auth.openai.com` e `api.openai.com`.
- Uma conta ChatGPT Plus/Pro elegível para autorizar o uso do plano nesta
  aplicação. A disponibilidade depende da conta, do workspace e das políticas
  da OpenAI. O login não garante quota ilimitada.
- Um browser no teu computador. Para uma VM remota, usa o túnel SSH do passo 3.

Há uma conta ativa por instância do bot. Os comandos, resumos e tarefas de perfis
usam essa conta e consomem a sua utilização autorizada. O painel permite guardar
registos de outras contas e escolher a conta ativa.

## 2. Configurar e arrancar

Atualiza os ficheiros do projeto na VM. No `.env`, mantém a configuração de
Discord, Postgres e transcrição e acrescenta:

```dotenv
LLM_PROVIDER=chatgpt
CHATGPT_ADMIN_PASSWORD=coloca_aqui_uma_password_longa_e_unica
CHATGPT_REDIRECT_URI=http://127.0.0.1:8000/auth/callback
CHATGPT_MODEL=
```

Podes gerar uma password com `openssl rand -hex 32` e copiar o resultado para
`CHATGPT_ADMIN_PASSWORD`. Esta password protege o painel e é também entregue
ao bot para autorizar a mudança de modelo por `/models`.

Deixa `CHATGPT_MODEL` vazio para escolher a partir do catálogo da conta. Depois
de selecionado, o modelo fica guardado; não é necessário editar o `.env` sempre
que mudas de modelo.

```bash
docker compose up -d --build
```

O Compose guarda as credenciais em
`/app/.tmp/state/chatgpt/auth.json`, dentro do volume `api_state`. A conta e o
modelo sobrevivem a reinícios e recriações dos containers. `docker compose down
-v` elimina esse estado juntamente com os outros volumes do projeto.

## 3. Abrir o painel numa VM

No computador em que vais abrir o browser, mantém este comando a correr:

```bash
ssh -N -L 127.0.0.1:8000:127.0.0.1:8000 commov@IP_DA_VM
```

Substitui `IP_DA_VM` pelo endereço da VM. Se `API_PORT` no `.env` for diferente
de 8000, substitui a **última** porta do túnel pela porta publicada na VM.

Abre **<http://127.0.0.1:8000/chatgpt>**. No pedido de autenticação do browser:

- Utilizador: `admin`
- Password: o valor de `CHATGPT_ADMIN_PASSWORD`

O callback de login tem de usar o endereço literal `127.0.0.1`. Abre o painel
por esse endereço, através do túnel, para que o browser regresse à mesma
instância que iniciou o login. Não substituas esse endereço por `localhost` ou
pelo IP da VM.

Se a porta local 8000 estiver ocupada, usa por exemplo 18000:

1. Configura `CHATGPT_REDIRECT_URI=http://127.0.0.1:18000/auth/callback` na VM.
2. Recria a API com `docker compose up -d --force-recreate api`.
3. Usa `ssh -N -L 127.0.0.1:18000:127.0.0.1:8000 commov@IP_DA_VM`.
4. Abre <http://127.0.0.1:18000/chatgpt>.

Para execução no próprio computador, abre diretamente o painel sem túnel.
Para execução sem Docker, `CHATGPT_AUTH_FILE` define o caminho local das
credenciais; o valor por omissão é `.tmp/chatgpt/auth.json`.

## 4. Fazer login e escolher o modelo

1. Clica em **Continue with ChatGPT**.
2. Inicia sessão na página da OpenAI e autoriza o uso do teu plano pela aplicação.
3. Regressa ao painel pelo link apresentado após o callback.
4. Escolhe um modelo na lista de modelos disponíveis à tua conta.
5. Clica em **Testar e ativar modelo**. A aplicação envia `Ola!` e só guarda a
   escolha quando recebe uma resposta concluída com sucesso.

O bot já tem o comando **`/models`**: lista os modelos da conta ativa e permite
testar e mudar o modelo diretamente no Discord. Mudar o modelo requer a
permissão **Gerir Servidor**, como nos outros fornecedores. A escolha aplica-se
a toda a instância do bot e fica guardada após um reinício. Os pedidos já em
curso podem terminar com o modelo que usavam quando começaram.

Os IDs não estão fixos no código: o catálogo é obtido com a sessão autenticada.
Ao mudar de conta, a escolha anterior é removida; escolhe um modelo disponível
à nova conta. Podes usar **Adicionar outra conta** ou voltar a entrar numa
conta guardada. Cada registo conserva o seu próprio identificador OAuth.

## 5. Sessão, utilização e logout

A API renova automaticamente a sessão, guardando os tokens substitutos em
conjunto. A renovação é serializada para evitar duas utilizações simultâneas de
um refresh token. Os ficheiros de credenciais são escritos atomicamente com
permissões `0600`; o browser recebe apenas o estado de login e o catálogo.

Usa uma instância Uvicorn para o login, como no Dockerfile do projeto: as
tentativas de login pendentes ficam em memória por 10 minutos. Se reiniciares
a API durante o login, inicia uma nova tentativa. A sessão já autenticada
continua persistente.

No painel, **Terminar sessão** tenta revogar a sessão na OpenAI e remove os
tokens locais, mantendo o registo da conta para um futuro login. Se a revogação
remota não puder ser confirmada, o painel informa-te. Nesse caso, desliga a
aplicação nas definições do ChatGPT. O browser pode manter a autenticação Basic
do painel até fechares a janela; essa autenticação é distinta da conta ChatGPT.

Podes gerir o acesso e os limites em <https://chatgpt.com/settings/usage>.
Os erros de quota ou inelegibilidade são apresentados; a aplicação não troca
automaticamente para uma API paga. O parâmetro `LLM_MAX_OUTPUT_TOKENS` não é
enviado como limite de resposta nesta integração, porque esse campo não é
suportado pela rota OAuth. Mantém-se o orçamento de contexto e as instruções
dos comandos existentes.

## 6. Resolver problemas

| Sintoma | Ação |
| --- | --- |
| Painel devolve 503 | Define `CHATGPT_ADMIN_PASSWORD` e recria API e bot. |
| Callback não chega à API | Confirma o túnel SSH, o endereço `127.0.0.1` e a porta de `CHATGPT_REDIRECT_URI`. |
| Login expirado ou estado inválido | Reinicia o login no painel; não reutilizes uma URL de callback. |
| Plano não autorizado | Volta a entrar e autoriza explicitamente o uso do plano. |
| Conta/plano inelegível | Confirma a elegibilidade e políticas da conta na OpenAI. |
| Limite de utilização atingido | Consulta a página de utilização; verifica os limites do plano e desta aplicação. |
| Sessão expirada ou revogada | Volta a entrar na conta guardada pelo painel. |
| `/models` não consegue ativar | Confirma a mesma `CHATGPT_ADMIN_PASSWORD` em API e bot; recria ambos se alteraste o `.env`. |
| `no route to host` | Corrige a rede/firewall Docker na VM; uma alteração ao fornecedor de IA não corrige esse erro. |

Os logs não incluem tokens OAuth nem os parâmetros do callback. Não copies
`auth.json` para o Git ou para conversas de suporte. Para recuperar uma sessão
do bot que falhou antes do setup, usa o comando `/retry` depois de confirmar
que o modelo está ativo e responde.

## Documentação oficial e validação

- [Login e registo OAuth](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Catálogo e inferência](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Renovação e contas](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Limitações da integração](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

A implementação usa PKCE, valida `state`, nonce, assinatura, emissor e audiência
do ID token, e só aceita inferência concluída pela Responses API. A validação
automática usa fornecedores simulados: o login real e a elegibilidade têm de
ser confirmados com a tua conta no servidor onde vais executar o projeto.
