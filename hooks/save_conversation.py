# Claude Code Stop hook — 세션 transcript(JSONL)를 sgmcp 서버로 업서트한다.
#
# 사용:
#   (hook)     stdin으로 {"session_id", "transcript_path", ...}를 받아 해당 세션 적재
#   (backfill) python hooks/save_conversation.py --backfill   # ~/.claude/projects/ 전체 일괄 적재
#
# 실패해도 항상 exit 0 — Claude Code 세션을 방해하지 않는다.
# 매 턴 전체 업서트라 누락된 턴은 다음 턴 실행이 자가 복구한다.

import json
import sys
from pathlib import Path

import httpx

SERVER = "https://sgmcp-seunggu.fly.dev"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
LOG_PATH = Path.home() / ".claude" / "sgmcp-hook.log"

# /clear 안내문 등 대화가 아닌 클라이언트 잡음
NOISE_MARKERS = ("<command-name>", "<local-command-stdout>")


def _token() -> str:
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("INGEST_TOKEN="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError(f"{ENV_PATH}에 INGEST_TOKEN이 없습니다")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        ).strip()
    return ""


def _extract(transcript_path: Path) -> tuple[list[dict], str]:
    """transcript에서 user/assistant의 순수 텍스트 턴만 추출한다."""
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
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        text = _text_of(entry.get("message", {}).get("content"))
        if not text or text.startswith("Caveat:") or any(m in text for m in NOISE_MARKERS):
            continue
        messages.append({"role": entry["type"], "content": text})
    return messages, cwd


def _push(session_id: str, transcript_path: Path) -> str:
    messages, cwd = _extract(transcript_path)
    if not messages:
        return f"{session_id}: 대화 없음, 건너뜀"
    payload = {
        "conversation_key": f"cc-{session_id}",
        "title": messages[0]["content"].splitlines()[0][:60],
        "messages": messages,
        "source": "claude-code",
        "project": Path(cwd).name if cwd else "",
    }
    response = httpx.post(
        f"{SERVER}/api/ingest",
        json=payload,
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=60,
    )
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
                print(f"{path.name}: 실패 — {e}")
        return
    hook_input = json.load(sys.stdin)
    _log(_push(hook_input["session_id"], Path(hook_input["transcript_path"])))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            _log(f"오류: {e}")
        except Exception:
            pass
    sys.exit(0)
