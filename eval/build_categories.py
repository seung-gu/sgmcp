"""Write the big-category answers for an eval version onto its branch: an LLM files every
conversation into big categories twice, and the naming eval scores placement on the
pairs both filings agree on. Run once per version (about 40 cents with gpt-5.5):

    uv run --group eval --env-file .env python eval/build_categories.py [--version v1]
"""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

# the answers belong on the eval branch, never production
os.environ["DATABASE_URL"] = os.environ["EVAL_DATABASE_URL"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import grouping  # noqa: E402
import groups  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from pydantic_ai import Agent, ModelRetry  # noqa: E402

MODEL = "openai:gpt-5.5"  # answers are written once, so the stronger model is worth it


class Shelf(BaseModel):
    name: str = Field(description="the category's name, 2-4 words")
    ids: list[int] = Field(description="numbers of the conversations in this category")


class Shelving(BaseModel):
    shelves: list[Shelf]


def file_once(rows: list[dict]) -> tuple[list[dict], object]:
    listing = "\n".join(f"[{i}] {r['title']} — {r['summary'][:300]}" for i, r in enumerate(rows))
    agent = Agent(MODEL, output_type=Shelving, retries=2)

    @agent.output_validator
    def check(ctx, output: Shelving) -> Shelving:
        if not grouping.MIN_BIG <= len(output.shelves) <= grouping.MAX_BIG:
            raise ModelRetry(f"Use {grouping.MIN_BIG}-{grouping.MAX_BIG} categories, not {len(output.shelves)}.")
        placed = [i for shelf in output.shelves for i in shelf.ids]
        missing = sorted(set(range(len(rows))) - set(placed))
        unknown = sorted(set(placed) - set(range(len(rows))))
        twice = sorted({i for i in placed if placed.count(i) > 1})
        if missing or unknown or twice:
            raise ModelRetry(
                f"Put every number in exactly one category; missing {missing}, "
                f"not in the list {unknown}, more than once {twice}."
            )
        return output

    result = agent.run_sync(
        "Below are one person's past AI conversations (title — summary). File every one "
        f"into {grouping.MIN_BIG}-{grouping.MAX_BIG} big categories, the way this person "
        "would shelve them. Categorize by what the person was doing in the conversation "
        "(e.g. building a piece of software, applying for jobs, dealing with an "
        "authority), not by words that merely appear in it. A category may stay small "
        "when its topic is unlike the others. Every number goes in exactly one "
        "category.\n\n" + listing
    )
    shelves = [{"name": s.name, "keys": [rows[i]["conversation_key"] for i in s.ids]} for s in result.output.shelves]
    return shelves, result.usage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="v1")
    args = parser.parse_args()
    rows = db.all_embedded(grouping.DRIFT_MIN_COS)
    with db._connect() as conn:
        if conn.execute(
            "SELECT 1 FROM eval_answers WHERE kind = 'categories' AND version = %s", (args.version,)
        ).fetchone():
            sys.exit(f"categories {args.version} already stored; nothing written")

    runs = []
    for n in (1, 2):
        shelves, usage = file_once(rows)
        runs.append(shelves)
        print(f"filing {n}: {len(shelves)} categories, {usage}")
        for shelf in sorted(shelves, key=lambda s: -len(s["keys"])):
            print(f"  {shelf['name']} ({len(shelf['keys'])})")

    together = [groups.pairs([s["keys"] for s in run]) for run in runs]
    both = together[0] & together[1]
    print(f"pairs kept together: {len(together[0])} and {len(together[1])}; by both {len(both)} "
          f"({len(both) / len(together[0] | together[1]):.0%} of either)")
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO eval_answers (kind, body, version) VALUES ('categories', %s, %s)",
            (json.dumps({"model": MODEL, "made": date.today().isoformat(),
                         "how": "an LLM filed every conversation into big categories twice, from title + summary",
                         "runs": runs}, ensure_ascii=False), args.version),
        )
    print(f"stored categories {args.version}")


if __name__ == "__main__":
    main()
