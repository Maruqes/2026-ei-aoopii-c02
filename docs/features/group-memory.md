# F06 — Group memory

Processes the conversation in bulks, storing topics, decisions and lore. It updates participants' profiles even without enabling the assistant. The stored history can be queried by the oracle.

**Configuration:** `GROUP_MEMORY_BULK_MINUTES` defines the bulk duration (5 minutes by default); `GROUP_MEMORY_CONTEXT_BULKS` chooses between 1 and 3 recent bulks for reaction context.

**Where it lives:**

- [app/group_memory.py](../../src/transcription-api/app/group_memory.py): periodic processing and memory/reaction routes.
- [data/group_memory.py](../../src/data/group_memory.py): bulk storage and pending work.
- [llm.py](../../src/transcription-api/app/llm.py): `analyze_group_bulk`.
- [011_group_memory.sql](../../src/data/migrations/011_group_memory.sql): memory schema.

**Connects to:** [profiles and lore](profiles-lore.md), [spontaneous reactions](spontaneous-reactions.md) and [oracle](oracle-quotes.md).
