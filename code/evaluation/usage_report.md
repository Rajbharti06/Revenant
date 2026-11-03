# Token Usage and Cost Report

Generated automatically by `python code/main.py` for the final full-dataset run that produced `output.csv` (518 requests).

| Provider | Model | API calls | Cache hits | Input tokens | Output tokens | Total tokens | Est. cost |
|---|---|---|---|---|---|---|---|
| - | (no model calls) | 0 | 0 | 0 | 0 | 0 | $0.0000 |
| **All** | **overall** | **0** | **0** | **0** | **0** | **0** | **$0.0000** |

- Requests: 518 (508 audited as deletions, 10 NOT_APPLICABLE)
- Average tokens per request: 0.0
- Estimated cost per request: $0.000000
- LLM available during this run (none provider configured): **False**

No API key was configured for the final run, so intent classification used the multilingual rule classifier and screenshots were read with local OCR (rapidocr) + pdftotext. Both are free and deterministic; the deterministic audit engine needs no model at all.

## Where models are used (when a key is set)

| Stage | Anthropic model | NVIDIA model | Calls for this dataset |
|---|---|---|---|
| Request-intent classification | `claude-haiku-5-5` | `nvidia/nemotron-3.5-lightning-30b-a3b` | ceil(518/40) = 13 batched calls |
| Screenshot / PDF fact extraction | `claude-sonnet-5-5` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | 15 (one per media file), cached in `code/cache/` |
| Identity, state reconstruction, policy, step planning | none (deterministic Python) | none | 0 |

Projected cost with an Anthropic key: roughly 0.13M tokens, about $0.25 for the whole dataset (~$0.0005 per request), using the prices in `code/llm.py`. NVIDIA's hosted free tier costs $0.
