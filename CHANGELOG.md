# Changelog

## 0.3.0 — Alpha workflow and resource improvements

- Expand to 44 tools with exact record lookup, scoped context, factual group statistics and a lightweight health check. Add explicit local-day, sender and type filters.
- Read QQ rows in bounded SQL batches using composite keyset cursors; retain legacy offset compatibility, stream native exports and refuse overwrite by default.
- Preserve ordered known QQ payload segments and repeated fragments; mark unknown private-protocol structures as partial. Protect JSONL cache updates with an OS lock and atomic writes.
- Add transactional export checkpoints, source-page revalidation, bounded merge and resumable media stages. Add an explicit local-ASR switch and precise metadata refresh for uncached media.
- Add one-command export/media/reader workflows, portable media packages, per-reference integrity manifests and bounded static reader pages.
- Decouple readable image previews from OCR availability; bound queues and merge identical in-flight speech jobs.
- Add opt-in authenticated per-user shared backends behind stdio bridges. The mode remains Alpha and requires deployment-specific multi-client validation.
- Keep limits explicit: live databases are not frozen snapshots, QQ private protocol coverage is incomplete, and existing per-chat JSONL cache merges still load that cached conversation.

## 0.2.1 — public source preview

- Package the local WeChat + QQ stdio gateway as an installable Windows Python project.
- Remove personal account defaults, private fixtures and fixed export batches.
- Move Python runtime data outside the source package using `WXQQ_DATA_DIR`.
- Keep QQ optional at startup; missing configuration is reported without selecting an account.
- Add documented client configuration, model provisioning, synthetic tests and a generic local reader.
- Retain 40 tools, source-aware pagination, media validation, OCR and optional local ASR.
- Publish source only; external reader binaries, database extensions and models are not bundled.

## 0.2.0 — local integration baseline

- Combined WeChat and QQ queries and added reliable local record identifiers.
- Added validated image/sticker paths, local OCR, voice recognition, and local video checks.
- Added cache-aware workers and idle process cleanup.

The local baseline was machine-specific. The public preview has a distinct configuration and data directory; it does not rewrite an existing local deployment automatically.
