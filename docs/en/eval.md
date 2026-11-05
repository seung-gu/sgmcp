[English](eval.md) | [한국어](../ko/eval.md)

# Evaluating search

`eval/run.py` runs two evals against answers that live, with the archive they were written against, on a Neon branch of their own (`EVAL_DATABASE_URL`). Scores don't move as new conversations arrive, and the answers stay out of this public repository.

| Eval | What it scores |
|---|---|
| meaning | meaning search on 62 reviewed questions: paraphrased, pinpoint, whole-conversation, absent-topic |
| groups | the small topic groups, on the conversation pairs two LLM filings agreed on |

Neither calls an LLM, so the same code always gets the same numbers. Each run is stored on the branch; the run accepted as the baseline is what later runs are compared with, and a regression exits 1.

```bash
uv run --group eval --env-file .env python eval/run.py            # score, compare with the baselines
uv run --group eval --env-file .env python eval/run.py --accept   # make this run the baseline
git config core.hooksPath .githooks   # run them before any push that changes search or grouping code
```

Keyword search has no eval: it either finds the string or it doesn't, so there is no value to tune by score.
