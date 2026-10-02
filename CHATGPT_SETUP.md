# ChatGPT: login, modelos e reasoning effort

Esta opção usa a conta ChatGPT para gerar resumos, atualizar perfis e responder
aos comandos de AI. A transcrição continua a usar `TRANSCRIPTION_PROVIDER`.
Há uma conta ativa por instância do bot.

## Configuração

Precisas de Docker Compose, Python 3 no computador onde executas `make`, uma
conta ChatGPT elegível para autorizar o uso do plano e acesso HTTPS a
`auth.openai.com` e `api.openai.com`. A elegibilidade, os modelos e os limites
dependem da conta e das políticas da OpenAI; esta opção não usa `OPENAI_API_KEY`.

Mantém as variáveis de Discord, Postgres e transcrição e acrescenta ao `.env`:

```dotenv
LLM_PROVIDER=chatgpt
CHATGPT_ADMIN_PASSWORD=coloca_aqui_um_segredo_longo_e_unico
CHATGPT_MODEL=
CHATGPT_REASONING_EFFORT=medium
CHATGPT_REDIRECT_URI=http://127.0.0.1:1455/auth/callback
```

Podes gerar o segredo com `openssl rand -hex 32`. `CHATGPT_ADMIN_PASSWORD`
autoriza os pedidos de mudança de modelo e effort do bot à API; não é a password
da conta ChatGPT e não é pedida no navegador.

Deixa `CHATGPT_MODEL` vazio para escolher um modelo no Discord através de
`/models`. `CHATGPT_REASONING_EFFORT` define o nível inicial. Uma escolha guardada
por `/models` ou `/effort` tem prioridade sobre o `.env` e sobrevive a reinícios.
Depois de editar o `.env`, recria os serviços:

```bash
docker compose up -d --build
```

## Login no próprio computador

Na raiz do projeto:

```bash
make codex
```

O comando constrói um pequeno container de login, abre o endereço oficial da
OpenAI no navegador do computador e aguarda a autorização. Faz login e autoriza
o uso do plano ChatGPT. Depois da confirmação, podes fechar o separador.
O comando não instala nem executa o Codex CLI: usa o fluxo oficial Sign in with
ChatGPT diretamente para este projeto.

O login funciona mesmo com a API parada. O callback escuta apenas em
`127.0.0.1:1455`; não há página de administração na porta 8000. O container de
login usa a rede do host em Linux, e termina quando o login acaba ou é cancelado.

As credenciais ficam em `/app/.tmp/state/chatgpt/auth.json`, no volume
`api_state`, partilhado com a API. A sessão renova-se automaticamente. O ficheiro
tem permissões restritas e não deve ser partilhado. `docker compose down -v`
apaga este volume, incluindo a sessão e as escolhas guardadas.

## Login na tua VM

No teu computador, abre um terminal e mantém este túnel ativo:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:1455:127.0.0.1:1455 commov@vm.commov
```

Noutro terminal, entra na VM e executa o login na pasta do projeto:

```bash
ssh commov@vm.commov
cd ~/2026-ei-aoopii-c02
make codex
```

Numa VM sem ambiente gráfico, o comando imprime o link. Copia-o para o navegador
do teu computador e faz login. O navegador regressa a `127.0.0.1:1455`, e o túnel
encaminha o callback para o processo de login na VM. Mantém o `make codex` ativo
até aparecer a confirmação; depois podes fechar o túnel.

Se a porta estiver ocupada, usa a mesma porta dos dois lados, por exemplo:

```bash
# No teu computador:
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:1456:127.0.0.1:1456 commov@vm.commov

# Na VM:
make codex CODEX_ARGS="--port 1456"
```

Para uma sessão já registada, pode mudar a porta; mantém o caminho
`/auth/callback`. Atualiza o antigo `CHATGPT_REDIRECT_URI` no `.env` se ainda
apontar para a porta 8000.

## Modelo e effort no Discord

Depois do login, usa `/models` para consultar o catálogo da conta, escolher um
modelo e testar um pedido curto antes de o guardar. A mudança de modelo requer
**Gerir Servidor**. O modelo selecionado passa a ser usado por todas as funções
de AI.

```text
/effort
/effort level:low
/effort level:medium
/effort level:high
/effort level:default
```

`/effort` mostra o modelo e o nível atuais. Com `level`, testa o nível proposto
no modelo ativo e só guarda a alteração se o pedido funcionar. O comando requer
**Gerir Servidor** e a alteração aplica-se aos próximos pedidos sem reiniciar.
Se ainda não escolheste um modelo, o teste usa o primeiro modelo do catálogo,
sem o selecionar. Assim podes corrigir a effort inicial antes de usar `/models`.

Os valores configuráveis são `default`, `none`, `minimal`, `low`, `medium`,
`high`, `xhigh` e `max`; cada modelo aceita apenas alguns deles. `default`
deixa o modelo escolher o seu nível. Se o modelo rejeitar o nível, o bot mantém
a escolha anterior. O `/models` também testa o modelo novo com a effort atual.

No `.env`, a configuração equivalente é:

```dotenv
CHATGPT_REASONING_EFFORT=low
```

Menor effort pode reduzir o tempo de resposta e o consumo de raciocínio.
Experimenta `low` para respostas mais rápidas, caso o modelo o aceite.
Para trocar para um modelo que não aceita a effort atual, usa primeiro
`/effort level:default`, depois `/models`, e escolhe o nível adequado.

## Consultar, trocar de conta e terminar sessão

```bash
make codex CODEX_ARGS="--status"
make codex CODEX_ARGS="--new-account"
make codex CODEX_ARGS="--account CLIENT_ID_GUARDADO"
make codex CODEX_ARGS="--logout"
```

`--status` mostra as contas guardadas e os seus IDs, sem tokens. `--new-account`
abre um novo login; `--account` ativa um registo existente. Ao mudar de conta,
a escolha guardada de modelo é removida: volta a escolher através de `/models`.
O nível de effort é mantido.

`--logout` tenta revogar a sessão ativa na OpenAI e remove os seus tokens locais.
Se a revogação remota não for confirmada, o comando informa-te. Podes gerir o
acesso e consultar os limites em [Utilização ChatGPT](https://chatgpt.com/settings/usage).

## Resolver problemas

| Sintoma | Ação |
| --- | --- |
| `channel open failed` no túnel | Executa `make codex` na VM antes de abrir o link; confirma que a porta do túnel coincide com a do callback. |
| Porta 1455 ocupada | Usa `--port 1456` e o túnel correspondente. |
| Login expira ou callback inválido | Executa `make codex` novamente e usa o link novo. |
| `/effort` ainda não aparece | Reconstrói e reinicia o bot para registar o novo comando Discord. |
| Modelo rejeita a effort | Usa um nível aceite ou `default`; a escolha anterior mantém-se. |
| `No route to host` / falha de DNS | Corrige a conectividade de saída da VM e dos containers. O login pela rede do host não corrige a rede da API. |
| Limite ChatGPT atingido | Consulta a utilização da conta e aguarda a reposição dos limites. |
| Sessão expirada ou revogada | Executa `make codex` novamente. |

O login e as mudanças não foram executados na tua conta durante os testes;
os testes usam respostas OAuth e de inferência simuladas.

## Referências oficiais

- [Registo e login](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Modelos e inferência com o plano ChatGPT](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Reasoning effort](https://developers.openai.com/api/docs/guides/reasoning)
