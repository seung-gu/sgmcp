"""Score meaning search against the eval branch, and compare with the accepted baseline.

Each version of the question set has its own Neon branch: a copy of the archive taken
when its questions were written, plus the eval_answers table holding them, which exists
on that branch only. Scoring runs there (EVAL_DATABASE_URL), so new conversations never
push an answer down and two runs on the same code give the same numbers. Every run is
kept in eval_reports on the same branch; one marked baseline is what later runs are
compared with, case by case.

    uv run --group eval --env-file .env python eval/run.py [--version v1]
        [--set MIN_SIMILARITY=0.35] [--accept]

--set overrides a grouping constant for this run, to try a value before changing it.
--accept makes this run the baseline. The exit code is 1 when a question that passed
in the baseline fails now.
"""
import argparse
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

# every query below must hit the eval branch, never production
os.environ["DATABASE_URL"] = os.environ["EVAL_DATABASE_URL"]
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db  # noqa: E402
import grouping  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402
from pydantic_evals import Case, Dataset  # noqa: E402
from pydantic_evals.evaluators import Evaluator, EvaluatorContext  # noqa: E402
from pydantic_evals.reporting import EvaluationReport  # noqa: E402

LIMIT = 10  # the search tool's default
# sessions that discuss this eval and quote its questions; they'd match them verbatim
EXCLUDED_KEYS = {"cc-b05198a8-a136-4a1b-b09d-16f37724eb96"}
TYPES = ("paraphrase", "pinpoint", "context", "absent")
RANKS = (1, 3, 5)  # an answer passes rankN when its conversation comes in the top N
# an absent question passes rankN when its top N results average below this; measured
# on v1, every real answer scored at least this, while the search floor
# (MIN_SIMILARITY) sits lower
ABSENT_BELOW = 0.35
SETTINGS = ("MIN_SIMILARITY", "SECTION_MARGIN", "SECTION_GAP", "MAX_SECTIONS")
REPORT = TypeAdapter(EvaluationReport)


def load(version: str) -> list[Case]:
    """The version's questions as cases; their stored vectors stand in for embedding
    calls, so a run costs nothing and doesn't move with the embedding API."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, body, vec::text AS vec FROM eval_answers"
            " WHERE kind = 'meaning' AND version = %s ORDER BY id",
            (version,),
        ).fetchall()
    stored = {
        r["body"]["query"]: json.loads(r["vec"])
        for r in rows
        if r["vec"] and r["body"]["embed_model"] == grouping.EMBED_MODEL
    }
    embed = grouping._embed_texts
    grouping._embed_texts = lambda texts: [stored.get(t) or embed([t])[0] for t in texts]
    return [
        Case(
            name=f"#{r['id']:02d}",
            inputs=r["body"]["query"],
            metadata={"type": r["body"]["type"]},
            expected_output=None if r["body"]["type"] == "absent"
            else {"conversation_key": r["body"]["conversation_key"], "idx": r["body"]["idx"]},
        )
        for r in rows
    ]


def search(query: str) -> list[dict]:
    """The task: what the meaning-search tool returns, minus the sessions about this eval."""
    return [
        {
            "key": r["conversation_key"],
            "title": r["title"],
            "similarity": round(r["similarity"], 4),
            "sections": [[s["start"], s["end"]] for s in r["sections"]],
        }
        for r in grouping.semantic_search(query, LIMIT)
        if r["conversation_key"] not in EXCLUDED_KEYS
    ]


@dataclasses.dataclass
class Ranked(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool]:
        if ctx.expected_output is None:
            sims = [r["similarity"] for r in ctx.output]
            # a slot the search left empty counts 0: returning less for an absent topic
            # is the point, so a stricter floor must not raise the average
            return {f"rank{n}": sum(sims[:n]) / n < ABSENT_BELOW for n in RANKS}
        keys = [r["key"] for r in ctx.output]
        key = ctx.expected_output["conversation_key"]
        rank = keys.index(key) + 1 if key in keys else None
        return {f"rank{n}": rank is not None and rank <= n for n in RANKS}


@dataclasses.dataclass
class SectionHit(Evaluator):
    """Whether the answer conversation's sections cover the answer message. Context
    answers are whole conversations and absent ones have none, so they aren't scored."""

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool]:
        expected = ctx.expected_output
        if not expected or expected["idx"] is None:
            return {}
        found = [r for r in ctx.output if r["key"] == expected["conversation_key"]]
        return {"section": bool(found) and any(a <= expected["idx"] <= b for a, b in found[0]["sections"])}


def passed(report: EvaluationReport) -> dict[tuple[str, str], bool]:
    return {(c.name, name): bool(a.value) for c in report.cases for name, a in c.assertions.items()}


def summary(report: EvaluationReport, baseline: EvaluationReport | None) -> None:
    now, before = passed(report), passed(baseline) if baseline else {}
    types = {c.name: c.metadata["type"] for c in report.cases}
    columns = [f"rank{n}" for n in RANKS] + ["section"]
    # each cell: the share passing, then how many more or fewer pass than in the baseline
    print(f"{'type':<11}" + "".join(f"{c:>15}{'':4}" for c in columns))
    for t in (*TYPES, "all"):
        cells = []
        for col in columns:
            keys = [k for k in now if k[1] == col and (types[k[0]] == t or (t == "all" and types[k[0]] != "absent"))]
            if not keys:
                cells.append(f"{'-':>15}{'':4}")
                continue
            hits = sum(now[k] for k in keys)
            delta = hits - sum(before.get(k, False) for k in keys) if baseline else 0
            cells.append(f"{f'{hits / len(keys):.0%} ({hits}/{len(keys)})':>15}{f' {delta:+d}' if delta else '':4}")
        print(f"{t:<11}" + "".join(cells))
    print(f"(absent passes rankN when its top N average similarity is below {ABSENT_BELOW})")


def git_commit() -> str:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "db.py", "grouping.py"], cwd=ROOT).returncode
    return head + ("+dirty" if dirty else "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="v1")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--accept", action="store_true", help="make this run the baseline")
    args = parser.parse_args()
    for item in args.set:
        name, value = item.split("=", 1)
        if not hasattr(grouping, name):
            sys.exit(f"grouping has no {name}")
        setattr(grouping, name, type(getattr(grouping, name))(value))

    dataset = Dataset(name=f"meaning-{args.version}", cases=load(args.version), evaluators=[Ranked(), SectionHit()])
    report = dataset.evaluate_sync(search, max_concurrency=8, progress=False)

    with db._connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS eval_reports (
                   id         serial PRIMARY KEY,
                   version    text NOT NULL,
                   made_at    timestamptz NOT NULL DEFAULT now(),
                   git_commit text NOT NULL,
                   settings   jsonb NOT NULL,
                   report     jsonb NOT NULL,
                   baseline   boolean NOT NULL DEFAULT false)"""
        )
        row = conn.execute(
            "SELECT id, git_commit, report FROM eval_reports WHERE version = %s AND baseline"
            " ORDER BY id DESC LIMIT 1",
            (args.version,),
        ).fetchone()
        settings = {name: getattr(grouping, name) for name in SETTINGS}
        conn.execute(
            "INSERT INTO eval_reports (version, git_commit, settings, report, baseline)"
            " VALUES (%s, %s, %s, %s, %s)",
            (args.version, git_commit(), json.dumps(settings),
             REPORT.dump_json(report).decode(), args.accept),
        )
    baseline = REPORT.validate_python(row["report"]) if row else None

    print(f"version {args.version}, {len(report.cases)} questions, settings {settings}")
    print(f"baseline: run #{row['id']} at {row['git_commit']}" if row else "baseline: none yet")
    summary(report, baseline)

    regressed = []
    if baseline:
        now, before = passed(report), passed(baseline)
        changed = {name for (name, col), ok in now.items() if before.get((name, col)) != ok}
        regressed = sorted({name for (name, col), ok in now.items() if before.get((name, col)) and not ok})
        if changed:
            print(f"\n{len(changed)} questions changed since the baseline ({len(regressed)} got worse);"
                  " assertions read rank1 rank3 rank5 section:")
            only = lambda r: dataclasses.replace(r, cases=[c for c in r.cases if c.name in changed])  # noqa: E731
            only(report).print(baseline=only(baseline), include_input=True, include_durations=False,
                               include_averages=False)
        else:
            print("\nno question changed since the baseline")
    if args.accept:
        print("this run is now the baseline")
    sys.exit(1 if regressed and not args.accept else 0)


if __name__ == "__main__":
    main()
