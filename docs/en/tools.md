[English](tools.md) | [한국어](../ko/tools.md)

# MCP tools

Endpoint `https://<your-app>.fly.dev/mcp`, streamable HTTP, WorkOS sign-in when `MCP_AUTH_ISSUER` is set.

| Tool | What it does |
|---|---|
| `save_conversation(messages, source?, project?, key?)` | Create a conversation (the server issues a `cw-`+UUID key when none is given), or overwrite one to repair it |
| `append_messages(key, new_messages, project?, source_url?)` | Append the messages not saved yet: the latest turn, plus any turn whose save was skipped. Messages already saved are stored once. `source_url` stores the chat's address for reopening |
| `get_conversation(key, full=False, start?, count?)` | Load one conversation, summary first. `full=true`, `start` or `count` adds the verbatim messages one page (about 20,000 characters) at a time: from `start`, or the most recent when omitted, so a short conversation comes back whole. `count` caps the number of messages; `next_start`/`prev_start` keep reading. Each message carries its time (`at`) |
| `list_conversations(limit)` | Recent conversations, without message bodies |
| `search_conversations(query, limit)` | Keyword search over title, summary and content; each result lists where the keyword occurs (`sections`, newest first) |
| `search_conversations_by_meaning(query, limit)` | Meaning search for a topic whose exact words are unknown; each result lists the stretches where it is discussed (`sections`). New messages become searchable seconds after they are saved |

## Sections
Each search result lists `sections`: the message ranges where the match sits.

| Field | Meaning |
|---|---|
| `start`, `end`, `length` | the message range and its size |
| `best_message` | the message that matched best (keyword search: the latest hit) |
| `excerpt` | a piece of that message |
| `start_at`, `end_at` | when the first and last message of the range were sent |

A long section means the topic ran for a while; a section of one or two messages was a passing remark. To read one, call `get_conversation` with `start` a few messages before `best_message`.

## Paging
`get_conversation` returns at most about 20,000 characters per call, which fits inside Claude Code's tool-output limit. `showing` is the range returned and `message_count` the total. Continue with `next_start` (forward) or `prev_start` (backward). A single message longer than a page comes back cut and marked `truncated`.

## Reopening a conversation
| Source | Stored as | Used how |
|---|---|---|
| claude.ai | the chat URL, which the assistant asks you to paste | a link on the dashboard |
| Claude Code | `cd <project> && claude --resume <session-id>` | a command with a copy button; works on the machine that ran the session, since it needs the local transcript |

The archive tools return the same value, so a conversation found later can be continued where it started.

Every search and read made through MCP lands in `recall_log`, which shows whether assistants follow a section through or stop at the first page.
