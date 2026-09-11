# Saved-conversation collector (DEV pilot)

Read new or updated saved ChatGPT conversations through the existing, isolated launcher session. The collector does not send prompts, change chats, or write project state. No API key, copied cookie, or extra browser profile is needed.

## Run

Start the source DEV launcher with `CODEX_WEB_GPT_HISTORY_ENABLED=1` in its environment. Sign in in the DEV app as usual. This opt-in endpoint is unavailable in production and Zero Risk mode. No UI tab needs to be open for collection once the session is signed in.

From the repository root:

```sh
python3 scripts/history/cli.py sync
python3 scripts/history/cli.py status
python3 scripts/history/cli.py review --vault /absolute/path/to/personal-vault
```

The first sync starts from now. To include a specific earlier window:

```sh
python3 scripts/history/cli.py sync --since 2026-09-08T00:00:00Z
```

For a small explicitly selected history sample, without changing the forward checkpoint:

```sh
python3 scripts/history/cli.py capture --id CONVERSATION_UUID
```

Use `--archive /absolute/private/path` and `--descriptor /absolute/path/to/launcher-browser.json` **before** the subcommand to override defaults. Defaults are:

- Archive: `~/.local/share/chatgpt-conversation-archive`
- DEV descriptor: `~/.local/share/codex-experiments/chatgpt-web/dev-profile/runtime/launcher-browser.json`

A normal `sync` catches up on **all new or updated conversations since the last completed checkpoint**, fetching 20 conversations per batch and continuing with further batches. Creation date does not limit discovery: an old thread updated today is included. Use `--batch-size N` to change the batch size (`--max-conversations` remains an alias for that per-batch setting).

Safety limits are `--max-batches` (default 25), `--max-pages` (100 pages of discovery depth per batch), and `--page-size` (20). Discovery depth grows as needed beyond the initial five pages. Reaching a limit returns `backlog_remaining: true` with `stop_reason: batch_limit` or `page_limit` and exits 2 without advancing the completed checkpoint. Reruns reuse successful captures; increase `--max-pages` if the window is deeper than its cap. A source failure also leaves the window pending. First-run and explicit-backfill boundaries survive process exit, so a later run cannot silently skip their remaining conversations.

`--refresh-known` verifies each overlapping known chat once per run even if its update timestamp has not changed. `capture --id` can force a recheck outside the current scan window. Only changed revisions produce new transcripts; repeat discovery does not duplicate messages. Explicit backfill can update bookkeeping even when content is unchanged.

## Data and correctness

- `archive.sqlite3` binds to a hashed account identity. A different signed-in user requires a separate archive. The current API's default account/workspace scope is used; workspace enumeration is not implemented.
- Each changed revision retains immutable raw JSON, SHA-256, source path, and acquisition start/finish times. Each new/changed source node is versioned once. Unchanged nodes are reused across revisions. Alternative branches remain in raw evidence.
- Readable Markdown in `transcripts/` follows `current_node` parent pointers. Hidden/system/reasoning and tool-execution messages are excluded. Missing branches, cycles, incomplete generations, and identity mismatches fail closed.
- Server message IDs, timestamps, attachments, citations, and content references are retained. Attachment **references** are captured; binaries are not downloaded. Unknown visible content types are labeled, with the source retained.
- Repeated unchanged ingestion does not rewrite the database or transcripts. Normal sync trusts stored update timestamps, uses a five-minute overlap, and advances its watermark only after the bounded window completes without errors. Exact rechecks are available with `--refresh-known` or `capture`.
- The list API's `total` may be a moving estimate (observed as offset + page size + 1). Pagination uses a short page or the time boundary, not that count. A detected ordering change aborts checkpoint advancement; rerun if the source changes during pagination.
- Capture/restart state is transactional. A process lock serializes writers. Files are owner-only. The pilot retains evidence until deliberately removed; it has no automatic deletion/expiry policy. Keep the archive outside synced/public directories.
- `review.json` and `review.md` suggest destinations. Exact IDs come from dedicated provenance fields in canonical projects, ideas, evergreens, or archives; redirect provenance resolves to its canonical target. Incidental links do not prove ownership. Non-exact suggestions use lexical overlap and always require review; they are not a trained semantic classifier.

## Local bridge feed

`python3 scripts/history/cli.py feed` returns a versioned JSON snapshot of the
already-captured visible conversations. It opens SQLite read-only, makes no network
requests, and does not acquire the collector writer lock or change checkpoints.
Run `sync` separately to refresh discovery. Consumers must treat conversation text
as data and maintain their own delivery state.

Version 1 includes `source`, hashed `account_key`, `completed_watermark`, explicit
coverage, and conversations with stable IDs, revision, title, URL, timestamps and
visible messages. Each message has its source ID, role, text, timestamp and attachment
count. Raw nodes, hidden reasoning, alternate branches, attachment URLs, credentials,
and vault relationships are excluded. A missing or unbound archive is an error.
Output contains private conversation text and must not be committed or logged.

## Coverage

Verified locally: regular saved chats, mobile-created chat discovery, continuation of an already captured chat, discovery of an old thread updated after the checkpoint, multiple catch-up batches, no-change repeat, and existing project ID matching. Access does not depend on visiting each chat in the browser. Older histories can be explicitly sampled.

Still limited: automatic discovery of chats visible only inside ChatGPT Projects or archived collections, exhaustive account history, edits that do not bump source update time outside the overlap, attachment binaries, and long-running operational reliability. Unit fixtures cover edits/branches; these were not live-edited in a personal conversation.

This is an **on-demand collector**, not a scheduled watcher. No scheduler, Full/MCP connector, normal Codex route, new memory setting, or automatic vault routing is installed.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/history -p 'test_*.py' -v
node --test launcher/tests/saved-history.test.cjs launcher/tests/control-server.test.cjs
```

The API adapter uses Electron's session network stack, only GETs fixed `chatgpt.com` endpoints, rejects redirects and oversized/non-JSON results, and never returns session tokens. The local control channel reuses the launcher's existing random bearer token and loopback listener; no new network listener is opened.

References: [Electron session.fetch](https://www.electronjs.org/docs/latest/api/session#sesfetchinput-init), [exporter API source](https://github.com/pionxzh/chatgpt-exporter/blob/master/src/api.ts). These are unofficial ChatGPT history endpoints; schema and coverage must be verified against the live account.
