# F07 — Reações espontâneas

O bot pode comentar ou fazer uma piada ligada à conversa por texto, voz e GIF, ou reagir apenas com GIF quando a imagem basta. A voz aguarda uma pausa e cede ao assistente, à música e aos participantes; nova fala cancela a intervenção por voz. Reações só com GIF nunca usam TTS e respeitam o mesmo cooldown.

**Configuração:** `GROUP_MEMORY_REACTIONS_ENABLED`, `GROUP_MEMORY_REACTION_COOLDOWN_MINUTES` (10 por defeito) e `GIPHY_API_KEY`. `/assistant disable` silencia as reações do servidor.

**Onde está:**

- [discord_bot/group_memory.go](../../discord_bot/group_memory.go): recolha, entrega, escolha de GIF e coordenação da voz.
- [app/group_memory.py](../../src/transcription-api/app/group_memory.py): criação, reserva e estado das reações.
- [llm.py](../../src/transcription-api/app/llm.py): avaliação da conversa e proposta de reação.

**Contexto:** depende da [memória do grupo](memoria-grupo.md). Sem chave GIPHY continua com texto/voz; sem cobertura Realtime completa publica no chat, sem voz. Não gera obrigatoriamente uma piada por bloco.

**GIFs:** pesquisa com palavras curtas em inglês (até 50 caracteres), rating `pg-13`, e escolhe entre os cinco melhores resultados novos. Usa uma versão animada compacta, ou a original quando necessário, e evita repetir os últimos 20 IDs por servidor, mesmo quando a URL muda. Se a primeira página estiver esgotada, tenta uma segunda; toda a pesquisa tem um limite de 5 segundos. O bot precisa de **Embed Links** no canal para que o Discord mostre a animação.

**Diagnóstico em produção:** procurar `group reaction GIF` nos logs do bot. Os eventos indicam se o modelo não propôs uma pesquisa, se falta configuração/permissão, se a GIPHY falhou (incluindo HTTP 403/429), se não há resultados novos ou se um GIF foi selecionado. Não incluem a chave nem a conversa. Uma falha GIPHY preserva o texto; sem texto nem GIF disponível, a reação termina em `failed` e não publica uma mensagem vazia. A claim continua a impedir duplicados.

O limite de pesquisa segue o [contrato Search da GIPHY](https://developers.giphy.com/docs/api/endpoint/search/); as versões animadas vêm de `images`, conforme o [schema GIF](https://developers.giphy.com/docs/api/schema/).
