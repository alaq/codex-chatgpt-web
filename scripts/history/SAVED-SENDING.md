# Saved-conversation sending (isolated DEV pilot)

The Matrix bridge can continue an existing saved ChatGPT conversation through a
separate authenticated loopback endpoint, `POST /v1/saved/send`. The normal task
adapter still requires Temporary Chats; its contract has not changed.

Start the isolated DEV launcher with these environment variables in addition to
its existing DEV profile configuration:

```sh
CODEX_WEB_GPT_HISTORY_ENABLED=1
CODEX_WEB_GPT_SAVED_SEND_ENABLED=1
CODEX_WEB_GPT_SAVED_SEND_DIR=/absolute/private/path/saved-send
```

The directory is owner-only state outside the repository. Keep it across restarts.
The capability is off unless explicitly enabled, requires automatic browser mode,
and reuses the signed-in DEV session without exporting credentials. It creates a
separate temporary browser window at the exact saved conversation URL and closes
that window after generation. It does not create Temporary Chats or select a
different web model. The observed live test used the conversation's existing model.

`scripts/history/send.py --descriptor /absolute/private/launcher-browser.json`
reads one JSON object from stdin. Prompts and credentials must not appear in argv:

```json
{
  "version": 1,
  "accountKey": "<verified 64-character account hash from the collector>",
  "conversationId": "<saved ChatGPT UUID>",
  "transactionId": "<stable 64-character transaction hash>",
  "text": "Plain text prompt"
}
```

Text must be non-empty and no larger than 12,000 UTF-8 bytes. Multiline drafts
use semantic paragraph/line-break readback instead of layout-derived `innerText`,
and plain-text insertion avoids editor Markdown shortcuts. An exact existing draft
from the same rejected request can be reused; a different draft blocks sending. The response has
`version: 1` and a status of `accepted`, `not_sent`, or `uncertain`. An accepted
response includes `userMessageId`, the saved ChatGPT user-message UUID. Acceptance
does not guarantee a successful assistant response; the collector independently
reads the completed visible conversation.

Before submitting, the sender verifies account, conversation, composer and source
head. It persists and fsyncs a journal before the single Send click. The journal
contains IDs, hashes and status, not prompt text or browser credentials. The sender
then checks for exactly one user child of the original head with matching text.
Lost responses can be reconciled with the same transaction and payload. Reusing
the transaction for different content is rejected. An unresolved prior attempt
blocks later sends to that conversation rather than clicking again. Do not delete
the journal or invent a new transaction to retry an uncertain send.

This is a UI-based pilot: source edits and simultaneous activity can make a send
uncertain. The Matrix service separately persists the original Matrix event before
calling this endpoint and restores its source association before mirroring, which
prevents normal and restart-related outbound echoes. Other callers must provide
their own durable association and authorization.

Validation: nine sender tests plus the unchanged launcher ownership/control tests;
live source acceptance, idempotent replay, and a Matrix-originated continuation of
the same saved conversation were verified on September 11, 2026.
