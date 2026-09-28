# reading-digest-pipeline

Generic pipeline that turns long-form talk content into readable localized documents.

What it does, per run:

1. reads the configured feed and builds a priority queue (newest first, then themed backfill groups);
2. fetches the matching public transcript from the configured transcript source;
3. translates it in chunks with free-tier models (whitelist enforced, no paid fallback);
4. renders a typeset `.docx` (CJK first-line indent, 1.5 line spacing);
5. imports the document into the user's cloud drive and moves it into the target folder;
6. verifies the result, records resumable state and reports a short summary.

All content-specific configuration (feed URL, transcript source, folder id, credentials) is injected
through GitHub Secrets; the repository stores only generic code, hashed item keys and counters.

## Configuration

Environment variables (all provided as secrets/vars):

| Name | Purpose |
| --- | --- |
| `FEED_URL` | RSS feed to poll |
| `TRANSCRIPT_BASE` | public transcript hub used to locate the matching page |
| `TARGET_FOLDER_ID` | destination folder id in the cloud drive |
| `SILICONFLOW_API_KEY` | key for the free-tier model provider |
| `TENCENT_DOCS_TOKEN` | token for the cloud document MCP endpoint |
| `WECOM_WEBHOOK` | optional progress notification webhook |
| `TRANSLATE_MODELS` | optional comma-separated override, must stay inside the free whitelist |

## Local usage

```bash
pip install -r requirements.txt
python run.py --limit 1 --dry-run --force   # translate + render only
python run.py --limit 1 --force             # full run: translate + upload
```

## Resumability

`state/state.json` tracks per-item stage (`fetched → translated → rendered → done`), attempts and errors.
Every run starts by reporting how much is already done, so interrupted runs continue where they stopped.
Scheduled runs are limited to a daily window (Asia/Shanghai 12:00–22:30); an item that starts before the
cutoff is always allowed to finish.
