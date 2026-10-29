"""Find and merge Claude Code conversations that are copies of each other.

Continuing a Claude Code session as a background job starts it with --fork-session:
a new session ID whose record begins with a copy of the old one, so the hook saves
the same messages under two keys. A copy keeps each message's text and time, so a
conversation whose first message (role, text, time) also sits inside another one is a
candidate, and the two are compared:

    contains   one holds the other's messages (all but a couple, or its last one):
               the smaller one goes
    continues  the later one starts partway into the earlier one (a session resumed
               after compaction) and holds its end: the earlier one's opening goes in
               front of the later one, and the earlier one goes
    diverged   each has messages the other lacks: both stay

Messages missing from the kept side are abandoned branches (a retried or interrupted
turn), which a copy leaves out. Each merge runs in its own transaction.

    uv run --env-file .env python scripts/merge_duplicates.py          # list what would merge
    uv run --env-file .env python scripts/merge_duplicates.py --apply  # merge
    ... --eval   on the eval branch, moving the answers that point at a removed conversation

A conversation that "continues" rewrote needs its search passages re-cut: in
production, press Refresh groups; on the eval branch, --eval --apply does it here.
Then re-accept the eval baseline (eval/run.py --accept), since the archive changed.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SLACK = 2  # messages one side may lack and still count as held (abandoned branches)


def messages(conn, cid: int) -> list[dict]:
    return conn.execute(
        "SELECT role, content, at FROM messages WHERE conversation_id = %s ORDER BY idx", (cid,)
    ).fetchall()


def text(m: dict) -> tuple[str, str]:
    return m["role"], m["content"]


def candidates(conn) -> list[tuple[int, int, int]]:
    """(earlier, later, k): the later conversation's first message is message k of the
    earlier one. Old rows have no times, so for them the text alone has to match."""
    return [
        (r["earlier"], r["later"], r["k"])
        for r in conn.execute(
            """
            SELECT m.conversation_id AS earlier, f.conversation_id AS later, min(m.idx) AS k
            FROM messages f
            JOIN conversations c ON c.id = f.conversation_id AND c.conversation_key LIKE 'cc-%%'
            JOIN messages m ON m.conversation_id <> f.conversation_id
                AND m.role = f.role AND m.content = f.content
                AND m.at IS NOT DISTINCT FROM f.at
            WHERE f.idx = 0
            GROUP BY 1, 2
            -- two conversations that start alike find each other at 0: list that pair once
            HAVING min(m.idx) > 0 OR m.conversation_id < f.conversation_id
            ORDER BY 1, 2
            """
        )
    ]


def held(inner: list[dict], outer: list[dict]) -> bool:
    """Whether `outer` holds `inner`, abandoned branches aside."""
    present = {text(m) for m in outer}
    missing = sum(text(m) not in present for m in inner)
    return missing <= SLACK or text(inner[-1]) in present


def verdict(earlier: list[dict], later: list[dict], k: int) -> tuple[str, str]:
    """(how, which side stays)."""
    if k == 0:
        if held(earlier, later):
            return "contains", "later"
        if held(later, earlier):
            return "contains", "earlier"
    else:
        if held(earlier[k:], later):
            return "continues", "later"
        if held(later, earlier[k:]):
            return "contains", "earlier"
    return "diverged", ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="merge (default: only list)")
    parser.add_argument("--eval", action="store_true", help="work on the eval branch (EVAL_DATABASE_URL)")
    args = parser.parse_args()

    with psycopg.connect(os.environ["EVAL_DATABASE_URL" if args.eval else "DATABASE_URL"], row_factory=dict_row) as conn:
        keys = {r["id"]: r["conversation_key"] for r in conn.execute("SELECT id, conversation_key FROM conversations")}
        removed: dict[str, tuple[str, int]] = {}  # removed key -> (kept key, positions the kept side's messages moved by)
        for a, b, k in candidates(conn):
            if keys.get(a) in removed or keys.get(b) in removed:
                continue  # already merged into something else this run
            earlier, later = messages(conn, a), messages(conn, b)
            how, stays = verdict(earlier, later, k)
            keep, drop = (b, a) if stays == "later" else (a, b)
            line = f"{a}({len(earlier)}) / {b}({len(later)}), from message {k}: {how}"
            print(line + (f" -> keep {keep}, remove {drop}" if stays else " -> leave both"))
            if not stays or not args.apply:
                continue
            with conn.transaction():
                shift = 0
                if how == "continues":
                    merged = earlier[:k] + later
                    conn.execute("DELETE FROM messages WHERE conversation_id = %s", (keep,))
                    with conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO messages (conversation_id, idx, role, content, at) VALUES (%s, %s, %s, %s, %s)",
                            [(keep, i, m["role"], m["content"], m["at"]) for i, m in enumerate(merged)],
                        )
                    # positions moved: re-cut the passages and rebuild the mean, as db._sync_messages does on a rewrite
                    conn.execute(
                        "UPDATE conversations SET message_count = %s, chunk_version = 0, embedding = NULL,"
                        " embedded_message_count = 0 WHERE id = %s",
                        (len(merged), keep),
                    )
                    shift = k
                conn.execute("DELETE FROM conversations WHERE id = %s", (drop,))
            removed[keys[drop]] = (keys[keep], shift)

        if args.eval and removed:
            move_answers(conn, removed)
    if not args.apply:
        print("(nothing changed; --apply merges)")
    elif any(shift for _, shift in removed.values()):
        if args.eval:
            reindex_eval_branch()
        else:
            print("rewritten conversations: press Refresh groups to re-cut their search passages")


def reindex_eval_branch() -> None:
    """The eval branch has no Refresh button, so cut the rewritten conversations'
    passages and average them here (embedding calls only, no LLM)."""
    os.environ["DATABASE_URL"] = os.environ["EVAL_DATABASE_URL"]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import grouping

    print(f"eval branch: {grouping._refresh_body_chunks()} passages embedded, {grouping._refresh_means()} means rebuilt")


def move_answers(conn, removed: dict[str, tuple[str, int]]) -> None:
    """Point eval answers at the kept conversations: keys move, and an answer's message
    index is found again by its quote, since positions shift in a merge."""
    kept_shift = {kept: shift for kept, shift in removed.values() if shift}
    with conn.transaction():
        for r in conn.execute("SELECT id, body FROM eval_answers WHERE kind = 'meaning'").fetchall():
            body = dict(r["body"])
            key = body.get("conversation_key")
            if key not in removed and key not in kept_shift:
                continue
            body["conversation_key"] = removed.get(key, (key, 0))[0]
            if body.get("quote"):
                found = conn.execute(
                    "SELECT m.idx FROM messages m JOIN conversations c ON c.id = m.conversation_id"
                    " WHERE c.conversation_key = %s AND m.content LIKE %s ORDER BY m.idx LIMIT 1",
                    (body["conversation_key"], f"%{body['quote']}%"),
                ).fetchone()
                body["idx"] = found["idx"] if found else None
            conn.execute("UPDATE eval_answers SET body = %s WHERE id = %s", (json.dumps(body, ensure_ascii=False), r["id"]))
            print(f"eval answer #{r['id']} -> {body['conversation_key']} message {body.get('idx')}")
        for r in conn.execute("SELECT id, body FROM eval_answers WHERE kind = 'groups'").fetchall():
            runs = [[list(dict.fromkeys(removed.get(k, (k, 0))[0] for k in folder)) for folder in run] for run in r["body"]["runs"]]
            conn.execute("UPDATE eval_answers SET body = %s WHERE id = %s",
                         (json.dumps({**r["body"], "runs": runs}, ensure_ascii=False), r["id"]))
            print(f"eval groups #{r['id']}: removed keys mapped to the kept ones")


if __name__ == "__main__":
    main()
