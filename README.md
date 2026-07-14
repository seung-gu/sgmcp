# sgmcp

A personal MCP (Model Context Protocol) server built on FastMCP, served over
streamable HTTP. It archives conversations from multiple LLM clients into one
database and auto-tags and groups them — see [ARCHITECTURE.md](ARCHITECTURE.md)
for the full design and its rationale.

## Tools

| Tool | Description |
|---|---|
| `save_conversation(messages, source?, project?, key?)` | create a conversation (server issues a `cw-`+UUID key when empty) or overwrite one wholesale (repair). Title/tags/summary are generated server-side |
| `append_messages(key, new_messages, project?)` | append only the latest turn — per-turn saving with a duplicate guard |
| `get_conversation(key, full=False)` | load one conversation. Summary-centric by default; `full=true` includes verbatim messages |
| `list_conversations(limit)` | recent conversations (message bodies excluded) |
| `search_conversations(query, limit)` | keyword search over title + summary + content |

## Running locally

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                                    # install dependencies
DATABASE_URL="postgresql://..." uv run python server.py    # serves on port 8000
```

To change the port:

```bash
PORT=8010 uv run python server.py
```

The MCP endpoint is `http://localhost:8000/mcp`. Smoke test:

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
```

## Docker

```bash
docker build -t sgmcp .
docker run -p 8000:8000 sgmcp
```

## Deployment (Fly.io)

```bash
fly deploy --ha=false
```

## Dashboard

The same server serves a web dashboard: `https://<host>/?token=<DASHBOARD_TOKEN>`

- conversation list, search, chat-style detail view
- group tree (big → small categories) and a 3D embedding projection (PCA)
- "Refresh groups": embeds new messages, then judges drift — if nothing drifted it
  stops there (groups/names/coordinates kept); otherwise it regenerates metadata for
  the drifted conversations, merges tags, re-clusters, has the LLM name/assign only
  the changed small clusters, and recomputes coordinates (requires `OPENAI_API_KEY`).
  The save paths (append/ingest) only store data.

## Claude Code auto-ingest (hook)

`hooks/save_conversation.py`, registered as a global Claude Code Stop hook, extracts
the conversation text from the session transcript (local JSONL) after every turn and
upserts it via `/api/ingest` — zero model tokens, 100% verbatim. Requires
`INGEST_TOKEN` in the local `.env`. Run with `--backfill` to push all past sessions.

## Environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string (e.g. Neon). Needed by the archive tools and dashboard |
| `DASHBOARD_TOKEN` | dashboard access token. Dashboard is disabled when unset |
| `INGEST_TOKEN` | auth token for `POST /api/ingest` (used by the Claude Code hook) |
| `OPENAI_API_KEY` | embeddings + metadata generation. List/search work without it |
| `ARCHIVE_LANGUAGE` | language for all derived text — titles, tags, summaries, group names (default: Korean) |

## Connecting to Claude

Do not put a trailing slash on the endpoint URL (`/mcp/` causes a 307 redirect and
the connection fails).

- **Claude Code**:

  ```bash
  claude mcp add --transport http sgmcp https://sgmcp-seunggu.fly.dev/mcp
  ```

- **claude.ai**: Settings → Connectors → Add custom connector → `https://sgmcp-seunggu.fly.dev/mcp`
