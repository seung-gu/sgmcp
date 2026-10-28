# Claude Code Stop hook — upserts the session transcript (JSONL) into the sgmcp server.
#
# Usage:
#   (hook)     receives {"session_id", "transcript_path", ...} on stdin, pushes that session
#   (backfill) python hooks/save_conversation.py --backfill   # push every past session
#
# Always exits 0 — it must never disturb the Claude Code session.
# Each turn sends only the session's last TAIL messages and the server adds the ones it
# lacks. When it lacks more (a run of failed turns) or holds something different, it
# answers 409 and the whole session goes instead, so any missed turn heals on the next.

import json
import sys
from pathlib import Path

import httpx

SERVER = "https://sgmcp-seunggu.fly.dev"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
LOG_PATH = Path.home() / ".claude" / "sgmcp-hook.log"

# Messages sent per turn. A turn adds a few; the rest overlap what the server already
# holds, which lets it check that it is extending the same list.
TAIL = 20

# client noise that is not conversation content (e.g. /clear banners)
NOISE_MARKERS = ("<command-name>", "<local-command-stdout>")


def _token() -> str:
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("INGEST_TOKEN="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError(f"INGEST_TOKEN not found in {ENV_PATH}")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        ).strip()
    return ""


def _extract(transcript_path: Path) -> tuple[list[dict], str]:
    """Pull only the user/assistant text turns out of a transcript."""
    messages: list[dict] = []
    cwd = ""
    for raw in transcript_path.read_text().splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cwd = entry.get("cwd") or cwd
        if entry.get("type") not in ("user", "assistant"):
            continue
        # API error notices (overload, outage, flagged turns) are written by the client,
        # not the model — storing them would only replay them into later conversations
        if entry.get("isSidechain") or entry.get("isMeta") or entry.get("isApiErrorMessage"):
            continue
        text = _text_of(entry.get("message", {}).get("content"))
        if not text or text.startswith("Caveat:") or any(m in text for m in NOISE_MARKERS):
            continue
        messages.append({"role": entry["type"], "content": text, "at": entry.get("timestamp")})
    return messages, cwd


def _post(payload: dict) -> httpx.Response:
    return httpx.post(
        f"{SERVER}/api/ingest",
        json=payload,
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=60,
    )


def _push(session_id: str, transcript_path: Path) -> str:
    messages, cwd = _extract(transcript_path)
    if not messages:
        return f"{session_id}: no conversation, skipped"
    # reopening needs the working directory too — `claude --resume` only finds
    # sessions belonging to the current directory
    resume = f"claude --resume {session_id}"
    if cwd:
        resume = f"cd {cwd} && {resume}"
    payload = {
        "conversation_key": f"cc-{session_id}",
        "source": "claude-code",
        "project": Path(cwd).name if cwd else "",
        # the dashboard shows source_url uniformly, so Claude Code fills in its
        # resume command here (claude.ai fills in a URL)
        "source_url": resume,
    }
    start = max(0, len(messages) - TAIL)
    response = _post({**payload, "messages": messages[start:], "start": start})
    if response.status_code == 409:
        response = _post({**payload, "messages": messages})
    response.raise_for_status()
    return f"{session_id}: {response.json()}"


def _log(message: str) -> None:
    with LOG_PATH.open("a") as f:
        f.write(message + "\n")


def main() -> None:
    if "--backfill" in sys.argv:
        transcripts = sorted((Path.home() / ".claude" / "projects").glob("*/*.jsonl"))
        for path in transcripts:
            try:
                print(_push(path.stem, path))
            except Exception as e:
                print(f"{path.name}: failed — {e}")
        return
    hook_input = json.load(sys.stdin)
    _log(_push(hook_input["session_id"], Path(hook_input["transcript_path"])))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            _log(f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
