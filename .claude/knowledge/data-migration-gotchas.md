---
category: Data
description: Pitfalls when converting data between formats (SQL dumps, YAML, JSON, CSV) for infrastructure-as-code migrations
file_patterns:
    - '**/scripts/**'
    - '**/convert*.py'
    - '**/migrate*.py'
    - '**/data/**'
keywords:
    - migration
    - data
    - sql
    - yaml
    - csv
    - conversion
    - etl
    - import
    - export
last_updated: "2026-08-15"
subcategory: Migration
technologies:
    - python
    - yaml
    - sql
    - terraform
topic: data-migration-gotchas
---
# Data Migration Gotchas

## SQL Dump Parsing

- Sanitize `\r\n` from string fields before matching/key generation. MySQL Windows dumps contain literal `\r\n` that silently breaks patterns and suffix stripping.
- Check for embedded garbage in record names (trailing whitespace, control chars, concatenated fields). Strip and validate before using as map keys.

## YAML Generation

- Detect and error on duplicate keys before writing. YAML parsers silently discard all but last definition → data loss with no warning.
- Multiple source records mapping to same YAML key with different values: merge into list or log warning — never silently drop.

## Python Conversion Scripts

- Never use `if prio:` or `if value:` for optional integer fields. Zero is falsy in Python → silently dropped. Use `if prio is not None:`.
- Always emit priority even when 0 (MX priority 0, TTL 0 are valid and different from omitting).

## Post-Migration Audit

- After migrating from any external source, run full audit pass over all generated config files.
- Attribute findings to source vs. script — distinguish original data problems from conversion-introduced problems.

## Promoted (2026-08-15)

- [PATTERN:0.90] Use asyncio.Lock-gated HTTP/2 client rebuild: on RemoteProtocolError or LocalProtocolError, atomically replace the h2 client so concurrent errors collapse into a single rebuild, and close the old client in the background.
- [PATTERN:0.80] When resolving identifiers from exported data, build an explicit ID→canonical-name map from a profiles/index file rather than deriving names from directory or display-name fields, which are non-unique.
- [GOTCHA:0.95] When a Facebook user deletes their account, their name is replaced everywhere in the data export with 'Facebook-Nutzer' — including old likes, comments, friend lists, and message threads. Reconstructing the real name is only possible if it appears as a plaintext tag, in a post body, or inside a message.
- [DECISION:0.85] When working with MindNode files programmatically, prefer the FreeMind (.mm) export over reverse-engineering the Protobuf Contents.data, as .mm is open XML with full hierarchy and content.
- [PATTERN:0.75] Use protoc --decode_raw to extract node hierarchy and text content from unknown Protobuf binaries when no .proto schema is available.
- [PATTERN:0.94] Before executing a large DB-to-API backfill, validate the hash pipeline by: (1) computing golden hashes from the real production code path (Kotlin) for a fixture set covering HTML entities, umlauts, CRLF, many notes, etc., (2) running the Python port over the same fixtures, (3) requiring 100% match before any writes. This catches serialization divergence (trim, separator, encoding) that only surfaces as spurious updates during the first incremental sync run.
- [PATTERN:0.90] For a resume-capable multi-shard backfill: use `--shard I/N` to partition ticket IDs across N independent processes, an account ID cache keyed by WHMCS client ID to eliminate repeated Espo lookups for the same client, and state-DB hash rows to skip unchanged records on resume. Transient port-forward failures on individual tickets can be retried with a targeted `--ticket ID` pass after the main run.
- [CONVENTION:0.85] Bank statement CSVs: use semicolon delimiter, decimal comma for amounts, UTF-8 with BOM, ISO date format (YYYY-MM-DD), sorted ascending by booking date — ensures correct auto-open behavior in Excel and Numbers.
- [PATTERN:0.85] Implement time-windowed backfill with a persisted watermark: store the reached 'since' date so re-runs extend the window by only changing the BACKFILL_SINCE env var, avoiding re-embedding already-processed records.
- [PATTERN:0.90] RAG backfill pipeline design: fetch all source records into PostgreSQL first (without vectors), then a separate embed job reads from PostgreSQL, embeds, quantizes, and writes vectors back to PostgreSQL and Qdrant — decouples ingestion from embedding and makes both stages independently restartable.
- [PATTERN:0.85] Store the complete raw API response as JSONB alongside typed columns to guarantee no data loss when the upstream API adds fields later.
- [GOTCHA:0.95] Using `.iso8601` date formatting discards sub-second fractions; two events in the same second can lose their ordering. Use `.iso8601` with `.withFractionalSeconds` or a custom formatter that preserves milliseconds.
- [GOTCHA:0.85] When a wire-protocol block model changes, a stale installed agent silently fails to decode fields (e.g. `.fileAction`); the handshake version stays the same so the mismatch goes undetected — coupling the protocol version to the model schema prevents silent regressions.
