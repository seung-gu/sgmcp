"""Meaning search: reviewed questions with known answers, scored on what the search
tool returns. Questions are paraphrased, pinpoint (one message holds the answer),
context (the whole conversation is the answer) or absent (nothing should match)."""
import json
from dataclasses import dataclass

import db
import grouping
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext
from pydantic_evals.reporting import EvaluationReport

LIMIT = 10  # the search tool's default
# sessions that discuss this eval and quote its questions; they'd match them verbatim
EXCLUDED_KEYS = {"cc-b05198a8-a136-4a1b-b09d-16f37724eb96"}
TYPES = ("paraphrase", "pinpoint", "context", "absent")
RANKS = (1, 3, 5)  # an answer passes rankN when its conversation comes in the top N
# an absent question passes rankN when its top N results average below this; measured
# on v1, every real answer scored at least this, while the search floor
# (MIN_SIMILARITY) sits lower
ABSENT_BELOW = 0.35
GATED_SCORES: tuple[str, ...] = ()  # assertions only; any flip counts


def dataset(version: str) -> Dataset:
    """The version's questions as cases. Their stored vectors stand in for embedding
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

    def embed_texts(texts: list[str]) -> list[list[float]]:
        missing = [t for t in texts if t not in stored]
        fresh = dict(zip(missing, embed(missing))) if missing else {}
        return [stored.get(t) or fresh[t] for t in texts]

    grouping._embed_texts = embed_texts
    cases = [
        Case(
            name=f"#{r['id']:02d}",
            inputs=r["body"]["query"],
            metadata={"type": r["body"]["type"]},
            expected_output=None if r["body"]["type"] == "absent"
            else {"conversation_key": r["body"]["conversation_key"], "idx": r["body"]["idx"]},
        )
        for r in rows
    ]
    return Dataset(name=f"meaning-{version}", cases=cases, evaluators=[Ranked(), SectionHit()])


def task(query: str) -> list[dict]:
    """What the meaning-search tool returns, minus the sessions about this eval."""
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


@dataclass
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


@dataclass
class SectionHit(Evaluator):
    """Whether the answer conversation's sections cover the answer message. Context
    answers are whole conversations and absent ones have none, so they aren't scored."""

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool]:
        expected = ctx.expected_output
        if not expected or expected["idx"] is None:
            return {}
        found = [r for r in ctx.output if r["key"] == expected["conversation_key"]]
        return {"section": bool(found) and any(a <= expected["idx"] <= b for a, b in found[0]["sections"])}


def summary(report: EvaluationReport, baseline: EvaluationReport | None) -> None:
    """Share passing per question type, with how many more or fewer pass than in the
    baseline; questions that changed are listed after this by run.py."""
    def passed(r: EvaluationReport) -> dict[tuple[str, str], bool]:
        return {(c.name, name): bool(a.value) for c in r.cases for name, a in c.assertions.items()}

    now, before = passed(report), passed(baseline) if baseline else {}
    types = {c.name: c.metadata["type"] for c in report.cases}
    columns = [f"rank{n}" for n in RANKS] + ["section"]
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
