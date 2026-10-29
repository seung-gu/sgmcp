"""Run the evals on the eval branch and compare each with its accepted baseline.

Each version of the eval set has its own Neon branch: a copy of the archive taken when
its answers were written, plus the eval_answers table holding them, which exists on
that branch only. Both evals run there (EVAL_DATABASE_URL), so new conversations never
move a score and the answers stay out of this public repository. Neither calls an LLM,
and the same code always gets the same numbers. Every run is kept in eval_reports on
the same branch; the last one accepted is the baseline later runs are compared with.

    meaning  meaning search on reviewed questions
    groups   the small topic groups against two LLM filings

    uv run --group eval --env-file .env python eval/run.py [--version v1]
        [--set MIN_SIMILARITY=0.35] [--accept]

--set overrides a grouping constant for this run, to try a value before changing it.
--accept makes this run the baseline. The exit code is 1 when an eval got worse than
its baseline: an assertion that held there fails now, or a gated score fell.
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
import groups  # noqa: E402
import meaning  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402
from pydantic_evals.reporting import EvaluationReport  # noqa: E402

EVALS = {"meaning": meaning, "groups": groups}
SETTINGS = ("MIN_SIMILARITY", "SECTION_MARGIN", "SECTION_GAP", "MAX_SECTIONS", "T_SMALL")
REPORT = TypeAdapter(EvaluationReport)


def results(report: EvaluationReport) -> tuple[dict, dict]:
    """Per case, each assertion's outcome and each score. A case whose task failed has neither."""
    held = {(c.name, k): bool(a.value) for c in report.cases for k, a in c.assertions.items()}
    scores = {(c.name, k): s.value for c in report.cases for k, s in c.scores.items()}
    return held, scores


def regressions(gated: tuple[str, ...], report: EvaluationReport, baseline: EvaluationReport) -> list[str]:
    held, scores = results(report)
    held_before, scores_before = results(baseline)
    worse = [f"{name}: {key} held, now fails" for (name, key), ok in held_before.items()
             if ok and not held.get((name, key), False)]
    for (name, key), before in scores_before.items():
        now = scores.get((name, key))
        if key in gated and (now is None or now < before - 1e-9):
            worse.append(f"{name}: {key} {before:.3f} -> {'failed' if now is None else f'{now:.3f}'}")
    return worse


def changed_cases(report: EvaluationReport, baseline: EvaluationReport) -> None:
    """pydantic-evals' side-by-side table, for the cases whose assertions changed."""
    held, held_before = results(report)[0], results(baseline)[0]
    changed = {name for (name, key), ok in held.items() if held_before.get((name, key)) != ok}
    if not changed:
        print("no question changed since the baseline")
        return
    print(f"{len(changed)} questions changed since the baseline; assertions read in the order above:")
    only = lambda r: dataclasses.replace(r, cases=[c for c in r.cases if c.name in changed])  # noqa: E731
    only(report).print(baseline=only(baseline), include_input=True, include_durations=False, include_averages=False)


def git_commit() -> str:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "db.py", "grouping.py", "eval/"], cwd=ROOT).returncode
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
    settings = {name: getattr(grouping, name) for name in SETTINGS}
    commit = git_commit()
    print(f"version {args.version} at {commit}, settings {settings}")

    with db._connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS eval_reports (
                   id         serial PRIMARY KEY,
                   version    text NOT NULL,
                   kind       text NOT NULL,
                   made_at    timestamptz NOT NULL DEFAULT now(),
                   git_commit text NOT NULL,
                   settings   jsonb NOT NULL,
                   report     jsonb NOT NULL,
                   baseline   boolean NOT NULL DEFAULT false)"""
        )

    worse = []
    for kind, module in EVALS.items():
        report = module.dataset(args.version).evaluate_sync(module.task, max_concurrency=8, progress=False)
        with db._connect() as conn:
            row = conn.execute(
                "SELECT id, git_commit, report FROM eval_reports"
                " WHERE version = %s AND kind = %s AND baseline ORDER BY id DESC LIMIT 1",
                (args.version, kind),
            ).fetchone()
            conn.execute(
                "INSERT INTO eval_reports (version, kind, git_commit, settings, report, baseline)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (args.version, kind, commit, json.dumps(settings), REPORT.dump_json(report).decode(), args.accept),
            )
        baseline = REPORT.validate_python(row["report"]) if row else None

        print(f"\n== {kind} (baseline: {f'run #{row['id']} at {row['git_commit']}' if row else 'none yet'})")
        for failure in report.failures:
            print(f"  FAILED {failure.name}: {failure.error_message}")
        module.summary(report, baseline)
        if baseline:
            if kind == "meaning":
                changed_cases(report, baseline)
            found = regressions(module.GATED_SCORES, report, baseline)
            worse += [f"{kind} {w}" for w in found]
            for w in found:
                print(f"  WORSE {w}")

    if args.accept:
        print("\nthis run is now the baseline")
    elif worse:
        print(f"\n{len(worse)} regressions against the baseline")
    sys.exit(1 if worse and not args.accept else 0)


if __name__ == "__main__":
    main()
