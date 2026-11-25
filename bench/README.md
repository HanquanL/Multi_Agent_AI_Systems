# Semantic reuse benchmark

`judge_eval.py` measures how well candidate retrieval (embedding similarity) and verification
(reject-only rules, then an LLM judge) separate question pairs whose answer can be reused from
pairs whose answer cannot. It calls the models directly, not the gateway.

    uv run python bench/judge_eval.py --judge-model qwen2.5:7b

## Labels

- `equivalent_intent`: both questions ask for the same thing.
- `direct_answer_reuse`: the truth used for scoring. It means an existing valid answer may
  satisfy both requests under the same model, context, generation parameters and reuse
  policy. It does not require independently generated answers to be textually identical, so
  open-ended requests (s14) and questions that rely on a convention (s05) can still be true.
- `relation`:
  - `same_intent`: reusable.
  - `different_intent`: not reusable.
  - `different_intent_same_answer`: the answers happen to match but the questions differ (d01);
    reuse would be right by accident, so it counts as a false reuse.
  - `convertible_output`: the same value in another unit (d26, d27); the value could be
    converted, not reused as is.
  - `shared_rule`: different inputs to one rule (d10); the rule could be shared, not the answer.

## Sets

- `pairs.jsonl` (v0.2, 58 pairs) is the development set. Thresholds and the judge prompt may be
  tuned on it, so results on it are not evidence of how the system performs elsewhere.
- A separate test set, written without looking at development results, is used once for the
  reported numbers. Its configuration (models, judge prompt, similarity floor) is recorded
  before it is run and is not changed afterwards.

## What these sets do not cover

They hold single short questions. Conversation history, generation parameters, workloads and
context specifications must be identical before two requests are compared at all; the
gateway's own tests cover that. Long inputs and outputs need their own benchmark: questions
longer than `max_question_chars` skip semantic matching (a size guard), which says nothing about
context windows, truncation or the cost of long requests. Requests with side effects are out
of scope; the gateway only generates text.

## Reporting

Report coverage, false reuse and precision at each similarity floor, with the number of pairs.
No false reuse on n negative pairs means none was observed in this set, not a zero error rate.