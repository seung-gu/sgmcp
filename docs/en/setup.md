[English](setup.md) | [한국어](../ko/setup.md)

# Setup

## Run locally
Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                                    # install dependencies
DATABASE_URL="postgresql://..." uv run python server.py    # serves on port 8000
```

Set `PORT` to use another port. The MCP endpoint is `http://localhost:8000/mcp`, and the dashboard is at `http://localhost:8000/dashboard`, behind a WorkOS sign-in. Smoke test:

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
```

The schema is created and updated on the first database connection. Statements that already ran are recorded in `schema_migrations` and skipped.

## Environment variables
| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string (e.g. Neon). Needed by the archive tools and the dashboard |
| `PUBLIC_URL` | The server's public address (default `http://localhost:8000`). The MCP resource and the sign-in callback are built from it |
| `MCP_AUTH_ISSUER` | WorkOS AuthKit domain. When set, `/mcp` takes only AuthKit access tokens; unset, `/mcp` is open |
| `MCP_AUTH_ORG` | WorkOS organization whose members may use `/mcp` and the dashboard: adding someone to it is the approval |
| `WORKOS_CLIENT_ID`, `WORKOS_API_KEY` | The WorkOS application the dashboard signs in through; register `<PUBLIC_URL>/auth/callback` as its redirect URI. Required for the dashboard |
| `WORKOS_COOKIE_PASSWORD` | Fernet key that seals the dashboard's sign-in cookies. Required for the dashboard |
| `INGEST_TOKEN` | Auth token for `POST /api/ingest`, used by the Claude Code hook |
| `OPENAI_API_KEY` | Embeddings, meaning search and metadata generation. List and keyword search work without it |
| `ARCHIVE_LANGUAGE` | Language of all derived text: titles, tags, summaries, group names (default: Korean) |
| `EVAL_DATABASE_URL` | Connection string of the eval branch (see [Evaluating search](eval.md)). Only the eval and its pre-push hook use it |

## Connect an app
Leave the trailing slash off the endpoint URL: `/mcp/` answers with a 307 redirect and the connection fails.

| App | How |
|---|---|
| Claude Code | `claude mcp add --transport http sgmcp https://<your-app>.fly.dev/mcp` |
| claude.ai | Settings → Connectors → Add custom connector → `https://<your-app>.fly.dev/mcp` |
| ChatGPT | add a custom connector with the same URL (developer mode) |

With `MCP_AUTH_ISSUER` set, an app opens a WorkOS sign-in the first time it connects (in Claude Code, run `/mcp` and pick the server). Only members of `MCP_AUTH_ORG` get in. The Claude Code hook below is not affected: `/api/ingest` keeps its own `INGEST_TOKEN`.

## Auto-save

### claude.ai and ChatGPT
An app calls the save tools only when asked to, so a few lines in its standing instructions do it on every conversation:

- claude.ai: Settings → General → "What personal preferences should Claude consider in responses?"
- ChatGPT: its custom instructions

An example to adapt:

```text
When the sgmcp connector is available:
- After your first answer in a conversation, save it with save_conversation and keep
  the conversation_key it returns.
- After every later answer, call append_messages with that key and the messages not
  saved yet, verbatim.
- When I refer to earlier work, search sgmcp first (search_conversations_by_meaning,
  or search_conversations for exact names) and read the matching section with
  get_conversation.
```

The server writes titles, tags and summaries itself; the app only sends and reads text.

### Claude Code
`hooks/save_conversation.py` runs as a global Stop hook. After every turn it sends the session's last 20 messages to `/api/ingest`, and the whole session only when the server is further behind. It reads the local transcript, so it spends no model tokens.

1. Put `INGEST_TOKEN` in the repo's `.env`, the same value as the server's.
2. Point `SERVER` at the top of `hooks/save_conversation.py` to your app.
3. Register the hook in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run --directory /path/to/sgmcp python /path/to/sgmcp/hooks/save_conversation.py",
            "timeout": 30,
            "async": true
          }
        ]
      }
    ]
  }
}
```

The hook always exits 0 and writes to `~/.claude/sgmcp-hook.log`, so a failure never interrupts a session; a missed turn is sent again with the next one. Run it with `--backfill` to push past sessions.

A session continued as a background job starts as a copy under a new ID, so the same messages get saved twice. `scripts/merge_duplicates.py` lists such copies and, with `--apply`, merges them.

## Deploy
```bash
docker build -t sgmcp . && docker run -p 8000:8000 sgmcp   # local container
fly deploy --ha=false                                      # Fly.io
```

Fly secrets: `DATABASE_URL`, `INGEST_TOKEN`, `OPENAI_API_KEY`, `PUBLIC_URL`, `MCP_AUTH_ISSUER`, `MCP_AUTH_ORG`, `WORKOS_CLIENT_ID`, `WORKOS_API_KEY`, `WORKOS_COOKIE_PASSWORD`.
