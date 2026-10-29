"""Names: the tree grouping._name_tree builds on the small groups of groups.clusters(),
one TREE_MODEL tree per repeat (about 2 cents with gpt-5.4-mini). Its output changes
from run to run, so scores get a tolerance and --repeat averages several trees.

- rules: no "prefix:" left in a name, MIN_BIG-MAX_BIG big categories, no two small
  groups sharing a name, names in ARCHIVE_LANGUAGE;
- placement: conversation pairs that share a big category, against the pairs two LLM
  filings into big categories (eval_answers kind 'categories') agreed on;
- name fit: how often a name, embedded, lands closest to its own members' cards, among
  all small groups (small names) or all big categories (big names).
"""
from collections import Counter
from dataclasses import dataclass

import db
import grouping
import groups
import numpy as np
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext
from pydantic_evals.reporting import EvaluationReport

SCORE_TOLERANCE = {"placement_f1": 0.10, "small_name_fit": 0.10, "big_name_fit": 0.10}
_state: dict = {}  # the version's agreed pairs, rows, clusters and card vectors, set by dataset()


def dataset(version: str) -> Dataset:
    with db._connect() as conn:
        body = conn.execute(
            "SELECT body FROM eval_answers WHERE kind = 'categories' AND version = %s", (version,)
        ).fetchone()["body"]
    rows, small = groups.clusters()
    means, cards = db.embeddings_by_id(), db.card_vectors_by_id()
    _state.update(
        pairs=groups.agreed([[shelf["keys"] for shelf in run] for run in body["runs"]]),
        rows=rows,
        small=small,
        # a name describes what its members are about, which the card says best
        vectors={r["conversation_key"]: cards.get(r["id"], means[r["id"]]) for r in rows},
    )
    return Dataset(
        name=f"naming-{version}",
        cases=[Case(name="naming", inputs=version)],
        evaluators=[Rules(), Placement(), NameFit()],
    )


def task(_: str) -> list[dict]:
    rows, small = _state["rows"], _state["small"]
    tree = grouping._name_tree(rows, small)
    return [
        {"small": name, "big": big, "keys": [rows[m]["conversation_key"] for m in members]}
        for (name, big), members in zip(tree, small)
    ]


def shelves(tree: list[dict]) -> dict[str, list[str]]:
    by_big: dict[str, list[str]] = {}
    for group in tree:
        by_big.setdefault(group["big"], []).extend(group["keys"])
    return by_big


@dataclass
class Rules(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool]:
        small = [g["small"] for g in ctx.output]
        names = small + list(shelves(ctx.output))
        return {
            "no_prefix": not any(":" in n for n in names),
            "big_count": grouping.MIN_BIG <= len(shelves(ctx.output)) <= grouping.MAX_BIG,
            "unique_small_names": len(set(small)) == len(small),
            "language": grouping._output_language_ok(" ".join(names)),
        }


@dataclass
class Placement(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> dict[str, float]:
        return {"placement_f1": groups.pair_scores(groups.pairs(list(shelves(ctx.output).values())), _state["pairs"])["f1"]}


def _fit(names: list[str], members: list[list[str]]) -> float:
    """Share of names whose embedding is closest to their own members' mean vector."""
    if len(names) < 2:
        return 1.0
    unit = lambda m: m / np.linalg.norm(m, axis=1, keepdims=True)  # noqa: E731
    named = unit(np.array(grouping._embed_texts(names), dtype=np.float32))
    centres = unit(np.array([np.mean([_state["vectors"][k] for k in keys], axis=0) for keys in members]))
    return float(np.mean(np.argmax(named @ centres.T, axis=1) == np.arange(len(names))))


@dataclass
class NameFit(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> dict[str, float]:
        by_big = shelves(ctx.output)
        return {
            "small_name_fit": _fit([g["small"] for g in ctx.output], [g["keys"] for g in ctx.output]),
            "big_name_fit": _fit(list(by_big), list(by_big.values())),
        }


def summary(report: EvaluationReport, baseline: EvaluationReport | None) -> None:
    def merged(r: EvaluationReport | None) -> tuple[dict, dict]:
        held, scores = {}, {}
        for case in r.cases if r else []:
            for name, a in case.assertions.items():
                held.setdefault(name, []).append(bool(a.value))
            for name, s in case.scores.items():
                scores.setdefault(name, []).append(s.value)
        return held, {k: sum(v) / len(v) for k, v in scores.items()}

    (held, scores), (_, before) = merged(report), merged(baseline)
    singles = [sum(1 for n in Counter(g["big"] for g in c.output).values() if n == 1) for c in report.cases]
    print(f"{grouping.TREE_MODEL}, {len(report.cases)} tree(s); big categories holding one group: {singles}")
    print("  rules  " + "  ".join(f"{k} {sum(v)}/{len(v)}" for k, v in held.items()))
    for name, value in scores.items():
        delta = f" ({value - before[name]:+.3f})" if name in before else ""
        print(f"  {name:<15}{value:.3f}{delta}")
    if report.cases:
        tree = report.cases[0].output
        print("  tree of the first repeat:")
        for big, keys in sorted(shelves(tree).items(), key=lambda kv: -len(kv[1])):
            names = [f"{g['small']} [{len(g['keys'])}]" for g in tree if g["big"] == big]
            print(f"    {big} ({len(keys)}): " + ", ".join(names))
