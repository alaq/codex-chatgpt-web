# Saved bridge extensions

These capabilities are isolated to the signed-in development profile. They do not
change the provider's Temporary Chat behavior or introduce an API model/router.
All control requests require the existing loopback descriptor token. Never put the
descriptor, archive, downloaded bytes or send journals in Git.

## Media

`scripts/history/media.py --descriptor …` reads an account/conversation/message/
attachment identity from stdin and calls `/v1/saved/media`. The backend verifies
that the attachment belongs to a visible message on the selected source branch,
then retrieves its signed download link using the source account. Only allowlisted
HTTPS file-storage hosts are fetched; bearer credentials are never forwarded to
storage. Responses contain bytes, size, name, MIME type and SHA-256, with a 20 MiB
limit. Metadata attachments, visible image pointers and visible sandbox download
links are supported. Expired assets or unsupported link formats fail explicitly.

Saved sends can include one attachment with `name`, `mimeType`, base64 `data` and
`sha256`. The backend checks bytes before staging a native browser File input,
waits for upload readiness, then performs the usual exact text/account/head checks.
The journal includes attachment hashes; the source receipt must match the expected
attachment metadata and returns its source IDs for outbound-echo suppression.

## New conversations

`scripts/history/create.py --descriptor …` calls `/v1/saved/create` with version 1,
`accountKey`, a 64-character hex `transactionId`, and `text` (1–12,000 UTF-8 bytes).
A private creation record is flushed before one native Send click. Repeated calls
reconcile that record and the observed saved conversation; they never blindly click
again after an uncertain result. The response identifies the accepted conversation
and user message. Separate deliberate commands have separate transactions.

The first message uses the existing saved ChatGPT UI model. A temporary-mode page,
unrelated draft, changed account or ambiguous creation is not accepted as success.
Existing browser verification/login requirements still apply.

## Progress and collection

`/v1/saved/status` exposes observed send/generation phases for a verified account and
conversation. The sender returns the durable user receipt before generation ends;
its page remains alive with a bounded wait. Timing out never implies completion.

`sync --bridge-progress` stages completed visible messages from unfinished turns in
private `bridge-progress/` snapshots. Partial answers and hidden reasoning are
excluded. The completed archive and its watermark stay behind an unfinished turn,
which is revisited on the next sync. A newly completed archive revision supersedes
any leftover progress snapshot. Work conversations marked `conversation_origin=tpp`
retain the existing conversation UUID, with `kind: work` in the visible feed.

The visible feed owns no vault project links, matches, approvals or project creation.

## Tests

```sh
node --test launcher/tests/saved-send.test.cjs launcher/tests/saved-create.test.cjs launcher/tests/saved-media.test.cjs
python3 -m unittest discover -s scripts/history -p 'test_*.py'
```

Tests cover lost receipts, restart, no second click, account/branch checks, signed
download credential isolation, attachment integrity and incomplete-turn visibility.
Browser selectors and the upstream download endpoint additionally require a live
profile check after source UI changes; passing fixtures alone is not that evidence.
