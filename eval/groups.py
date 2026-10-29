"""Topic grouping: the small groups Refresh would make from the eval branch, scored on
conversation pairs that two LLM filings (eval_answers kind 'groups') agreed on: kept
together by both, or kept apart by both. Pairs they disagreed on aren't scored."""
from dataclasses import dataclass

import db
import grouping
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext
from pydantic_evals.reporting import EvaluationReport

# the clustering is deterministic, so any drop in F1 is a real one
SCORE_TOLERANCE = {"f1": 0.0}
_pairs: dict[str, set[tuple[str, str]]] = {}  # the version's agreed pairs, set by dataset()


def pairs(groups: list[list[str]]) -> set[tuple[str, str]]:
    return {(a, b) if a < b else (b, a) for g in groups for a in g for b in g if a != b}


def agreed(runs: list[list[list[str]]]) -> dict[str, set[tuple[str, str]]]:
    """Pairs both runs keep together, and pairs both keep apart."""
    keys = sorted({k for g in runs[0] for k in g})
    together = [pairs(run) for run in runs]
    every = {(a, b) for i, a in enumerate(keys) for b in keys[i + 1:]}
    return {"together": set.intersection(*together), "apart": every - set.union(*together)}


def clusters() -> tuple[list[dict], list[list[int]]]:
    """The rows and small clusters exactly as grouping.refresh computes them."""
    rows = db.all_embedded(grouping.DRIFT_MIN_COS)
    matrix = grouping._center_by_language(grouping._load_matrix(rows), rows)
    return rows, grouping._agglomerate(matrix, grouping.T_SMALL)


def dataset(version: str) -> Dataset:
    with db._connect() as conn:
        body = conn.execute(
            "SELECT body FROM eval_answers WHERE kind = 'groups' AND version = %s", (version,)
        ).fetchone()["body"]
    # kept off the evaluator's fields, which every stored report would repeat
    _pairs.update(agreed(body["runs"]))
    return Dataset(name=f"groups-{version}", cases=[Case(name="groups", inputs=version)], evaluators=[PairF1()])


def task(_: str) -> list[list[str]]:
    rows, small = clusters()
    return [[rows[m]["conversation_key"] for m in members] for members in small]


def pair_scores(got: set[tuple[str, str]], answer: dict[str, set[tuple[str, str]]]) -> dict[str, float]:
    """Precision over the scored pairs a grouping puts together, recall over the pairs
    that belong together, and their F1."""
    hits = len(got & answer["together"])
    precision = hits / max(hits + len(got & answer["apart"]), 1)
    recall = hits / len(answer["together"])
    return {"f1": 2 * precision * recall / max(precision + recall, 1e-9), "precision": precision, "recall": recall}


@dataclass
class PairF1(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> dict[str, float]:
        return pair_scores(pairs(ctx.output), _pairs)


def summary(report: EvaluationReport, baseline: EvaluationReport | None) -> None:
    case = report.cases[0]
    before = baseline.cases[0].scores if baseline and baseline.cases else {}
    sizes = sorted((len(g) for g in case.output), reverse=True)
    print(f"T_SMALL {grouping.T_SMALL}: {len(sizes)} groups, {sizes.count(1)} alone, largest {sizes[0]}"
          f" (pairs scored: {len(_pairs['together'])} together, {len(_pairs['apart'])} apart)")
    for name in ("f1", "precision", "recall"):
        now = case.scores[name].value
        delta = f" ({now - before[name].value:+.3f})" if name in before else ""
        print(f"  {name:<10}{now:.3f}{delta}")
