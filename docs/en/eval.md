[English](eval.md) | [한국어](../ko/eval.md)

# Evaluating search and grouping

Two evals score meaning search and topic grouping, so a change to `db.py`, `grouping.py` or `eval/` shows whether it made results better or worse.

## Where the answers live
The answers sit on a Neon branch of their own (`EVAL_DATABASE_URL`), next to a copy of the archive taken when they were written. Both evals run there:

- New conversations never move a score.
- The answers quote personal conversations, so they stay out of this public repository.
- Neither eval calls an LLM, and questions are stored with their embeddings. A run costs nothing, and the same code always gets the same numbers.

Every run is stored in `eval_reports` on that branch. The run accepted with `--accept` is the baseline that later runs are compared with.

## Running
```bash
uv run --group eval --env-file .env python eval/run.py                      # score, compare with the baselines
uv run --group eval --env-file .env python eval/run.py --accept             # make this run the baseline
uv run --group eval --env-file .env python eval/run.py --set T_SMALL=0.30   # try a grouping constant first
git config core.hooksPath .githooks                                         # run before pushes of search code
```

The pre-push hook runs the evals only when `db.py`, `grouping.py` or `eval/` changed, and stops the push when a score got worse or the eval crashed. `git push --no-verify` pushes past it once.

## meaning: the right conversation, at the right place
62 reviewed questions, each with the conversation that answers it and, for most, the message.

| Type | Questions | Answer |
|---|---|---|
| paraphrase | 20 | a conversation, asked about in other words |
| pinpoint | 20 | one message that holds the answer |
| context | 12 | a whole conversation |
| absent | 10 | nothing: the topic is not in the archive |

| Score | Passes when |
|---|---|
| rank1, rank3, rank5 | the answer conversation is in the top 1, 3 or 5 results. An absent question passes when those results average a similarity below 0.35 |
| section | the result's sections cover the answer message (paraphrase and pinpoint only) |

A question that passed in the baseline and fails now counts as a regression.

## groups: the right conversations together
Two separate LLM filings of the same archive gave two groupings. The pairs of conversations both kept together, and the pairs both kept apart, are the answers; pairs they disagreed on are not scored. The eval rebuilds the small groups exactly as Refresh groups does and scores those pairs:

| Score | Meaning |
|---|---|
| precision | of the pairs it put together, the share that belong together |
| recall | of the pairs that belong together, the share it put together |
| F1 | the two combined; a drop counts as a regression |

## Current results
A run on 2026-11-05, the same as the accepted baseline.

| meaning | rank1 | rank3 | rank5 | section |
|---|---|---|---|---|
| paraphrase | 90% | 95% | 100% | 95% |
| pinpoint | 75% | 95% | 100% | 95% |
| context | 83% | 100% | 100% | - |
| all but absent | 83% | 96% | 100% | 95% |
| absent | 30% | 30% | 50% | - |

Absent questions are the weak spot. For 7 of the 10, the top result still scores 0.35 or more, so search returns a confident-looking answer for a topic that is not there.

groups: 36 groups (16 of one conversation, the largest of 7), scored on 120 pairs that belong together and 2,899 that don't. F1 0.553, precision 0.696, recall 0.458.

## What the measurements decided
These measurements were made while building search and grouping in July 2026, before the current question set existed. The values they settled are listed in [ARCHITECTURE.md](../../ARCHITECTURE.md#tuned-values-and-their-evidence-measured).

### Search passages, not averages
On 66 queries with known answers, the right conversation came first:

| Unit searched | First result right |
|---|---|
| one average vector per conversation | 48% |
| single messages | 59% |
| 1,500-character passages | 68% (70% on a later live re-check) |

An average says what a whole conversation is about. Grouping needs that, but it is too blurred for search, and one conversation ended up near the middle of everything and won unrelated queries. A single message often lacks the context around it. Passages sit in between, and their vectors take 24 MB where one per message took about 65 MB.

### Weight the average by length
Grouping still uses one vector per conversation: the average of its passages. With every message weighted equally, short meta remarks made up 37% of a conversation's characters but cast 53% of the vote, and the conversation landed in the wrong group. Each passage now counts by its own length.

### Geometry for "are these similar?", an LLM for "what kind of thing is this?"
Vector distance finds conversations about the same thing, so the small groups come from geometry. Big categories need knowledge: object detection and multi-agent systems are both AI, though their vectors are far apart. Grouping the big categories by geometry alone left 7 near-singleton groups.

An LLM now names them in two calls: it invents the categories with what each holds, then places every small group in one. One call doing both filed software projects under job hunting. On 36 groups the two-call version misfiled about 6 with `gpt-4.1-mini`, 5 with `gpt-5.4-mini` and 3 with `gpt-5.5`, at $0.01, $0.02 and $0.18 a regroup. sgmcp uses `gpt-5.4-mini`.
