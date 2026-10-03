# Speechmatics: consumo e créditos

Investigação em 2026-10-03. Foram feitas apenas consultas GET à Speechmatics;
não foi enviado áudio, criado nenhum job nem alterada a configuração. A chave
fornecida não está incluída neste documento.

## Resultado

- A Usage API pública documentada reporta **Batch**, não Realtime nem saldo.
- O portal mostra Realtime e Batch em Usage, e créditos em Billing.
- Não foi encontrado um endpoint público documentado para consultar créditos.
  Isto não prova que não exista uma API privada ou contratual.
- Na investigação adicional foi encontrada a rota interna do portal
  `GET https://portal.speechmatics.com/api/credits`. O cliente lê `data.credits`.
  A rota devolveu HTTP 302 para `/login`, tanto sem autenticação como com a chave
  de transcrição em `Authorization: Bearer`. Falta uma sessão autenticada do
  portal para validar o saldo devolvido.
- A Management API documentada gere projetos e chaves, com um management token;
  não documenta operações de saldo ou billing.
- Há uma diferença importante entre a documentação e o comportamento observado:
  `until` explícito com a data de hoje devolveu jobs de hoje em EU1.

## Consultas reais com a chave fornecida

| Endpoint/período | HTTP | Resultado Batch |
| --- | --- | --- |
| EU1 `/v2/usage`, sem datas | 200 | 107 jobs; 37.328484 h; 2026-06-19 a 2026-10-02 |
| EU1 `since=2026-10-01&until=2026-10-02` | 200 | `summary=null`, `details=null` |
| EU1 `since=2026-10-01&until=2026-10-03` | 200 | 4 jobs; 0.08828889 h |
| EU1 `since=2026-10-03&until=2026-10-03` | 200 | Os mesmos 4 jobs; 0.08828889 h |
| EU1 `since=2026-10-02&until=2026-10-02` | 200 | `summary=null`, `details=null` |
| EU1 `since=2026-10-02&until=2026-10-03` | 200 | Os mesmos 4 jobs; 0.08828889 h |
| EU2 `/v2/usage`, sem datas | 401 | Sem acesso com esta chave |
| US1 `/v2/usage`, sem datas | 200 | `summary=null`, `details=null`; início 1970-01-01 |

EU1 é `https://eu1.asr.api.speechmatics.com`. Os 107 jobs representam cerca de
37h 19m 43s; os 4 de hoje, cerca de 5m 18s, todos Melia 1. O histórico em EU1
inclui 106 jobs Melia 1 e 1 Enhanced. As respostas de usage não continham campos
de preço, saldo ou créditos, nem headers de crédito/saldo/quota/usage.

O resultado vazio em US1 demonstra que mudar de endpoint pode mudar o consumo
visível; não deve ser apresentado como saldo livre da conta. O endpoint do portal
redirecionou para login; o saldo autenticado não foi observado.

A documentação diz que o dia atual é excluído. Os testes acima demonstram que
EU1 aceita e retorna dados desse dia quando `until` é explícito. Não foi medida
a latência de atualização e não se pode garantir este comportamento noutras
regiões ou contas.

## Causa da apresentação fraca no projeto

1. `app/speechmatics_usage.py:fetch_speechmatics_usage` força `until=ontem`.
   `app/main.py:get_speechmatics_key_usages` acrescenta apenas WAVs Batch locais
   concluídos hoje. O `/keys` recebe e mostra apenas esse consumo Batch.
2. Existe `/v1/speechmatics/realtime-usage`, mas o `/keys` não o utiliza.
3. `app/realtime.py:bridge` só persiste `realtime_seconds` ao terminar/falhar uma
   unidade, através de `DataRepository.finish_realtime_unit`. Uma unidade ativa
   não atualiza o contador persistido; um crash pode perder esse intervalo.
4. `DataRepository.local_realtime_hours` agrega todo o histórico, sem filtro por
   período; o Batch usa o mês UTC atual por omissão. Os valores não são comparáveis
   sem alinhar as datas.
5. O contador de frames aumenta antes de `upstream.send` terminar. Para estimar
   consumo, é preferível contar envios concluídos, reconhecendo que enviar áudio
   não prova faturação pelo fornecedor.

## Alteração mínima proposta

- Mostrar Batch e Realtime separadamente no `/keys`, com segundos e período.
- Para Batch, permitir consulta explícita até hoje e identificar os dados de hoje
  como provisórios. Não somar WAVs locais de hoje ao mesmo total: isso duplicaria
  jobs já reportados. Se for necessário complementar atrasos, usar uma estimativa
  separada, sem a apresentar como total reconciliado.
- Para Realtime, guardar periodicamente o total cumulativo de áudio enviado,
  por unidade/key, e fazer um último flush no fecho. Reutilizar `realtime_seconds`,
  sem tabela nova. A atualização e o fecho devem preservar o maior valor observado
  para evitar que um checkpoint atrasado faça o contador recuar.
- Filtrar Realtime pelo período apresentado, distinguindo consumo local de
  reporte oficial. Uma gravação que vai de Realtime para fallback Batch pode
  consumir os dois produtos; não eliminar uma das parcelas como duplicado.
- Mostrar créditos como **indisponíveis pela API pública consultada**, com link
  para Billing. Não converter um orçamento de horas em saldo de créditos.
- Se for preciso uma estimativa monetária, usar saldo inicial confirmado no
  portal e tarifas/descontos da conta. A fórmula é saldo inicial menos consumo
  desde a data dessa leitura, com tarifa própria por produto/modelo. Uso de outras
  aplicações, grants, expiracões e alterações de preço impedem um saldo exato só
  com os contadores locais.
- Várias chaves do mesmo workspace podem partilhar o saldo; não somar saldos ou
  atribuir um grant independente a cada chave.

## Fontes oficiais

- [Usage: API apenas Batch; portal Realtime e Batch](https://docs.speechmatics.com/administration/usage)
- [Usage reporting: datas UTC e omissão de `until`](https://docs.speechmatics.com/speech-to-text/batch/usage)
- [Referência GET /usage](https://docs.speechmatics.com/api-ref/batch/get-usage-statistics)
- [Billing: créditos, PAYG e necessidade de papel Admin](https://docs.speechmatics.com/administration/billing)
- [Management API: projetos e API keys](https://docs.speechmatics.com/api-ref/management/management-api)
- [Management tokens: permissões disponíveis](https://docs.speechmatics.com/administration/management-tokens)
- [Projects: chaves por projeto e billing partilhado](https://docs.speechmatics.com/administration/projects)
- [Autenticação e endpoints regionais](https://docs.speechmatics.com/get-started/authentication)

A Billing documentada usa 1 crédito = 1 USD. O grant documentado para contas
anteriores a 2026-08-01 é de 25 USD, e para novos registos de 100 USD. Estes valores
não permitem inferir o saldo desta conta; não foi consultado o seu Billing.

## API interna do portal: descoberta adicional

Os assets públicos do portal confirmam uma via para consultar créditos:

- [Manifest de rotas observado](https://portal.speechmatics.com/assets/manifest-3686e8af.js)
  declara `routes/_authenticated.api.credits`, com path `api/credits` e loader
  no servidor. A página Billing tem path `/settings/workspace/billing`.
- [Hook useCredits observado](https://portal.speechmatics.com/assets/useCredits-B4mjfwb_.js)
  chama `/api/credits` com o fetcher do React Router e lê `data.credits`.
  O valor inicial vem de `metronomeCredits` no loader da rota autenticada.
  Isto identifica o nome dos campos usados pelo cliente, não a unidade/tipo
  exatos da resposta, que ainda não foi obtida autenticada.
- O hook atualiza o saldo ao recuperar foco/visibilidade e, após um evento de
  refresh, aos 5, 30, 60 e 120 segundos. Não é um contador instantâneo de streaming.
- Os módulos públicos do Billing mostram `credits available`, pagamentos e
  subscriptions. Os loaders do servidor não estão presentes nesses módulos,
  pelo que não foi possível identificar a chamada upstream de billing.
- Teste GET `/api/credits` com `Accept: application/json`, sem credencial: 302
  para `/login`, corpo vazio.
- O mesmo GET com a chave fornecida em `Authorization: Bearer`: exatamente
  o mesmo 302. Nesta rota, a chave API de transcrição não substitui o login.
- O navegador disponível também mostrou a página de login. Foi aberta para
  autenticação pelo utilizador; não se obteve nem exportou nenhuma sessão.

Próximo passo concreto: depois do login, abrir `/api/credits` no mesmo navegador
e verificar a resposta visível, o saldo, a unidade e o workspace. Se a sessão
autorizar essa consulta, há uma via interna para ler o saldo. A integração num
serviço precisaria de autenticação de sessão suportada e renovação; a descoberta
não demonstra uma API estável com API key ou management token. Não guardar
cookies de sessão no Git nem pedir credenciais/cookies pelo chat.
