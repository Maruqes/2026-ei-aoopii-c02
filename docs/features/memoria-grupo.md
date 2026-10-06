# F06 — Memória do grupo

Processa a conversa em blocos, guardando temas, decisões e lore. Atualiza os perfis dos participantes mesmo sem ativar o assistente. O histórico guardado pode ser consultado pelo oráculo.

**Configuração:** `GROUP_MEMORY_BULK_MINUTES` define a duração do bloco (5 minutos por defeito); `GROUP_MEMORY_CONTEXT_BULKS` escolhe entre 1 e 3 blocos recentes para contexto das reações.

**Onde está:**

- [app/group_memory.py](../../src/transcription-api/app/group_memory.py): processamento periódico e rotas de memória/reações.
- [data/group_memory.py](../../src/data/group_memory.py): armazenamento dos blocos e trabalho pendente.
- [llm.py](../../src/transcription-api/app/llm.py): `analyze_group_bulk`.
- [011_group_memory.sql](../../src/data/migrations/011_group_memory.sql): esquema da memória.

**Liga-se a:** [perfis e lore](perfis-lore.md), [reações espontâneas](reacoes-espontaneas.md) e [oráculo](oraculo-citacoes.md).
