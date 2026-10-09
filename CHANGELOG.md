# Changelog

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
