# sgmcp Architecture

A personal conversation archive: it collects conversations scattered across LLM
clients into one database, auto-derives titles, tags and summaries, and groups
them by topic.

## Architecture

```mermaid
graph LR
    subgraph clients [Clients]
        A["claude.ai<br/>preferences + MCP tools"]
        C["ChatGPT<br/>when connected"]
        B["Claude Code<br/>global Stop hook"]
    end

    subgraph sgmcp["sgmcp — Fly.io Frankfurt, suspends when idle"]
        M["/mcp<br/>MCP endpoint"]
        I["/api/ingest<br/>write door, INGEST_TOKEN"]
        D["Dashboard<br/>DASHBOARD_TOKEN"]
        R["Refresh groups<br/>derivation pipeline"]
    end

    DB[("Neon Postgres<br/>one table: conversations")]
    O["OpenAI<br/>embeddings + gpt-4.1-mini"]
    U((User))

    A -- "save / append<br/>verbatim" --> M
    C -- "save / append" --> M
    B -- "transcript upsert" --> I
    M --> DB
    I --> DB
    U -- "browse · search · refresh" --> D
    D --> R
    R <--> DB
    R -- "only what drifted" --> O
```

One line per component:

| Component | Role |
|---|---|
| Clients | **pipes** — they push verbatim text under a key, nothing else. No judgment |
| Server (refresh) | all derivation — titles/tags/summaries, embeddings, groups, coordinates |
| Neon | the only store. Only `messages` is canonical; everything else is recomputable |
| OpenAI | embeddings (text-embedding-3-small), generation/naming (gpt-4.1-mini via pydantic-ai, schema + language guardrails with retry) |

## Save workflows

### claude.ai (per turn, driven by preferences)

```mermaid
sequenceDiagram
    participant F as claude.ai model
    participant S as sgmcp
    participant DB as Neon

    Note over F: conversation starts — first answer done
    F->>S: save_conversation (empty key, first turn verbatim)
    S->>DB: INSERT — server issues the key (cw-UUID)
    S-->>F: returns conversation_key

    loop after every answer
        F->>S: append_messages (that key, latest turn verbatim)
        S->>DB: append (identical resend ignored)
    end

    Note over F,S: lost key → recover via list_conversations<br/>invented keys are rejected with guidance
```

### Claude Code (per turn, hook-driven — zero model tokens)

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant H as Stop hook (local script)
    participant S as sgmcp /api/ingest
    participant DB as Neon

    CC->>H: turn ends (transcript path on stdin)
    H->>H: extract conversation text from JSONL<br/>(tool output and internal logs dropped)
    H->>S: full upsert (cc-sessionUUID, token header)
    S->>DB: replace wholesale
    Note over H,DB: full-state upsert every turn = any failure<br/>heals on the next turn; 100% verbatim
```

## Refresh-groups workflow

The dashboard button is the only derivation trigger; save paths only store. 🤖 marks
an LLM call — with N drifted conversations a refresh makes **N + 2** calls, with
zero drift it makes **none**.

```mermaid
flowchart TD
    S([Refresh groups]) --> E["embed new messages only<br/>skip under 30 chars · length-weighted mean"]
    E --> G{"drift check<br/>no anchor, or cosine below 0.99"}
    G -- "none" --> X(["done — groups, names, coordinates all kept<br/>zero LLM calls"])
    G -- "one or more" --> M["🤖 regenerate drifted conversations only<br/>sample of 40 → title · tags (importance-ordered) · summary<br/>anchor moves to the current vector"]
    M --> T["🤖 tag merge — one global call<br/>unify same topic, different spellings"]
    T --> K["small clusters (geometry)<br/>per-language centering · cosine 0.35"]
    K --> CH{"per cluster:<br/>same members as last time,<br/>none regenerated?"}
    CH -- "kept" --> KEEP["name and big category unchanged<br/>(appears only in the candidate roster)"]
    CH -- "changed" --> N["🤖 name and assign — one global call<br/>changed clusters' raw tags + roster (names, top tags)<br/>fallback: most-common tag, own category"]
    KEEP --> P["3D coordinates (PCA) → store everything"]
    N --> P
```

Sample = split the conversation into 20 segments, take the longest user and
assistant message from each (max 40 messages, 400 chars each).

## Data model — three layers of columns

| Layer | Columns | Written by |
|---|---|---|
| Canonical (immutable) | `conversation_key` `messages` `source` `project` | clients |
| Derived — LLM | `title` `tags` (importance-ordered array) `summary` `tagged_embedding` (anchor) | refresh, drift-gated |
| Derived — math | `embedding` (weighted mean) `embedded_*_count` `cluster_small/big(_label)` `px py pz` | refresh |

## Design principles

1. **The verbatim text is the only truth** — every derivative can be reset and
   recomputed (this happened several times during development)
2. **Store everything, filter only in derivation** — dropping data at save time is
   irreversible, so it is never done
3. **Clients are pipes** — all judgment (titles, tags, groups) lives on the server;
   reliance on model instruction-following is minimized
4. **Match the tool to the question** — "are these similar?" is math (geometry);
   "are these the same kind of thing, and what do we call it?" is an LLM's job
5. **Show only what the judgment needs** — originals for the decision subject,
   digests for context, nothing for the unaffected

## Tuned values and their evidence (measured)

| Value | Evidence |
|---|---|
| drift threshold cos 0.99 | last-10% growth ≈ 0.999 (skip) / doubling ≈ 0.983–0.989 (fire) / 25% foreign-topic inflow ≈ 0.980–0.995. A false fire costs one cheap call |
| length-weighted mean | equal weighting over-represents short frequent messages — real case: meta remarks were 37% of characters but took 53% of the vote and misplaced a conversation |
| per-language centering | before: similarity median 0.714 (one blob). After: same topic 0.4–0.9, different topics negative |
| big categories = LLM | "object detection and multi-agent are both AI" is classification knowledge, not geometry — the geometric version failed with 7 near-singletons |
| small-cluster threshold 0.35 | placed in the measured valley of the centered similarity distribution |
| `ARCHIVE_LANGUAGE` | all derived text in one configured language — keeps the tag vocabulary from splitting per language; enforced by an output validator with retry |

## Operations

- **Secrets** (Fly): `DATABASE_URL` `DASHBOARD_TOKEN` `INGEST_TOKEN` `OPENAI_API_KEY`;
  local `.env`: `INGEST_TOKEN` (for the hook)
- **Deploy**: `fly deploy --ha=false` — schema migrates automatically at startup
- **Cost**: Fly under $2 (suspend) + Neon $0 + OpenAI a few dozen cents per month
- **Full big-category rebuild** (one-off, when the structure grows stale):
  `UPDATE conversations SET cluster_small = NULL`, then refresh

## Known limitations / open items

- **`/mcp` is unauthenticated** — anyone with the URL can read stored conversations.
  The price of claude.ai connector compatibility; an unresolved security item
- claude.ai-side verbatim text can't be guaranteed (the model retypes it; the client
  offers no raw-transcript channel)
- A conversation mixing two topics lands between them — segmentation is deferred
  (it's a derivative, so it can be added retroactively)
- Unchanged groups never get re-assigned — use the full rebuild above when stale
- Conversations with only short messages (< 30 chars) have no embedding and don't
  appear in groups or on the map
