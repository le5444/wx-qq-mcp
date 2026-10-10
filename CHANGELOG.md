# Changelog

## 0.3.3 — Windowless Windows daemon startup

- Use the same environment's `pythonw.exe` for shared-daemon bootstrap. A console venv launcher can spawn its base interpreter without preserving WMI hidden/detached flags and open an empty Windows Terminal window.
- Normalize `python.exe` and `pythonw.exe` within the same environment for profile identity. Stdio bridges retain console Python and their protocol streams; only the detached daemon is windowless.
- Refuse to fall back to a console daemon if `pythonw.exe` is missing. Add Windows process-level checks for absence of a console, protocol health, identity reuse and shutdown.

## 0.3.2 — Scoped discovery and consistent context reads

- Let chat discovery select WeChat, QQ or both and forward WeChat chat-type filters. Invalid/contradictory selectors fail before contacting a reader; unrequested platforms stay idle.
- Centralize time parsing for unified and native QQ reads. Explicit empty dates/bounds, reversed ranges, ambiguous eight-digit dates and silently rounded fractional seconds are rejected. A valid future range clipped by the snapshot is still a legitimate empty interval.
- Validate cursor structure before reading and reject invalid source timestamps instead of coercing booleans/floats into history order.
- Revalidate the located context target across both read passes, including the complete target second. Changed/duplicate targets, scan-cap ambiguity and inconsistent bidirectional neighbors return partial with the original target preserved.
- Expand server/tool guidance and add examples for person, group, date, all-history, keyword and media requests. These instructions and deterministic tests are not a measured LLM routing-accuracy claim.

## 0.3.1 — Identity and coverage hardening

- Require exact QQ UID/UIN and owner-profile matches, reject conflicting selectors, and scope self-chat reads to the actual conversation. Unknown or contradictory sender identities remain unassigned.
- Bind verified QQ numeric-account aliases to canonical UIDs. Native QQ statistics use stable identity keys instead of names; sender filters separate identity namespaces.
- Validate bounded pages before exposing any records. Rejected source pages no longer carry foreign chat text in diagnostic output; source-degradation warnings cannot become complete absence or complete group statistics.
- Limit voice resolution to current-message resources and exact cache identity; preserve outer chat scope, reject contradictory media metadata and same-variant QQ cache collisions.
- Validate external enrichment against immutable message identity and merge only derived fields. Exported collision record IDs can be used for exact lookup and context without selecting the first ambiguous record.
- Compatibility changes: native QQ `by_sender` uses `uid:`/`uin:` keys with display names supplied separately. Dual-platform sender queries use `wechat_sender` and `qq_sender`. Incomplete media identities and fuzzy stable-ID lookups are rejected rather than guessed.

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
