# Boundary semantics

This document states exactly what the scanner concludes, what it refuses to
conclude, and why. When a check cannot be performed, the tool records that
fact in a dedicated section rather than reporting success.

## 1. What a "candidate" means

A candidate means: **some bytes in this snapshot match a structural rule and
clear that rule's entropy/length/keyword gates**. That is all.

It does **not** mean:

* the value is a real, live, valid or active credential;
* the value grants access to anything;
* the value was leaked maliciously rather than committed intentionally;
* the value is current or expired.

There is intentionally **no network validation** step. Nothing in the codebase
performs HTTP/DNS/TLS calls to credential providers, and adding such calls is
out of scope. The `confidence` field (`low|medium|high`) describes the
strength of the *shape* evidence (e.g. a checksummed provider prefix is
stronger than a generic `token = "..."` assignment), not whether the secret
works.

## 2. Coverage — scanned vs not scanned

Every path appears in the inventory exactly once:

| Inventory status | Meaning |
|---|---|
| `scanned` | Contents were read and the rules were applied (media recorded) |
| `ignored` | Excluded on purpose by a versioned scope pattern; still listed so coverage is auditable |
| `oversize` | Size `>= max_file_bytes`; the file was **never opened**, explicitly reported as NOT SCANNED (`file_too_large`) |
| `symlink-skipped` | Symlinks are not followed (they could point outside the snapshot); NOT SCANNED (`symlink_not_followed`) |
| `unreadable` | `stat`/`read` failed; NOT SCANNED (`file_unreadable`) and listed under `failures` |

"Not scanned" is never folded into "clean". A scan over a tree containing an
oversize file is a scan with reduced coverage, and the report's counts say so.

## 3. Text vs binary

* NUL byte present → **binary** (`nul-byte-present`).
* Otherwise strict UTF-8 decode succeeds → **text**.
* Otherwise → **binary** (`not-valid-utf8`); binary scanning extracts
  printable-ASCII runs (minimum length is a scope setting) and applies the
  same rules. Binary matches report **byte offsets**, never line numbers;
  `line` is `null`.

Binary extraction cannot find secrets encoded in non-ASCII-compatible forms
(e.g. UTF-16, base64 split across runs below the minimum, compression,
encryption, or bespoke obfuscation). Such omissions are a known coverage
limit, not errors.

## 4. Lifecycle states between scans

A finding's identity is the pair `(content fingerprint, rule id)`. Diffing
two scans yields:

| State | Exact meaning |
|---|---|
| `new` | No previous completed scan contained this fingerprint+rule |
| `open` | Present again at the same path set (still there) |
| `moved` | Same content, but at least one occurrence path is new (rename/move — the finding is unresolved) |
| `known_fixed` | Content is absent **and** the previous path still exists with **different contents**. This is the strongest offline evidence of remediation; it is NOT a verified claim that the credential was rotated or revoked — see §1 |
| `uncertain_removal` | Content is absent but the old path is gone (`old_path_gone`) or newly ignored (`old_path_now_ignored`), or the file content is unchanged while no rule matches (typically a rule-pack change). Relocation cannot be ruled out, so it is not called "fixed" |
| `baseline_exempt` | Content matches a reviewed, content-fingerprint-bound baseline entry in this scan |

"Same file, same SHA-256, but no match" is never a fix: it normally means a
rule was weakened/removed between scans and is flagged uncertain.

## 5. Baselines

An exemption is `(fingerprint[, rule_id])`. Matching is by **content only**;
file names and paths are not consulted. The baseline records the pepper id
(`sha256(pepper)[:12]`) and refuses to load under a different pepper, because
its fingerprints could not be reproduced and silently exempting unknown
content would be unsafe.

A baseline exemption is not a statement that the content is harmless; it is an
operational acknowledgement ("we accept this finding for now") made by a human
and tracked in the audit trail.

## 6. Data minimisation

* Stored per finding: mask, HMAC fingerprint, rule id, confidence, positions,
  masked evidence window, file SHA-256, timestamps.
* Never stored: the raw value. The `Secret` wrapper renders as its mask in
  `str()`/`repr()`/f-strings; raw bytes exist transiently in engine memory
  only.
* A `RedactingFilter` additionally replaces registered raw values in any log
  statement, including accidental ones; registrations are reset per scan.

HMAC fingerprints prevent trivial reversal by anyone without the pepper, but
a determined holder of the pepper can brute-force low-entropy candidate
spaces. Protect the pepper like any other secret.

## 7. Identity and explainability

* Every request carries `X-Request-Id` (generated as `req-…` when absent) and
  `X-Actor-Id` (default `local-cli`). Both appear on every DB audit row and
  every JSON log line.
* Every report records the exact rule-pack version+hash, scope-pack
  version+hash, pepper id and baseline path used, plus per-file processing
  status/reason and per-candidate positions.
* Failures (`failures`) and uncertain conclusions (`uncertainties`) are
  separate top-level sections with machine-readable `reason_code`s.

## 8. Deliberately NOT checked / not performed

These are presented as boundaries, never as passing checks:

1. **No live credential validation** — no network calls at all.
2. **No git history scanning** — the input is a snapshot (working tree);
   secrets in history packs are out of scope unless those packs are extracted
   and presented as snapshots.
3. **No decoding pipelines** — base64/hex-decoded blobs, compressed or
   encrypted files, exotic encodings, image/document metadata are not
   transformed before matching.
4. **No semantic judgement** — whether a candidate is intentional
   (test fixture, documentation, canary token) is a human decision; the
   baseline mechanism supports the workflow but does not make it.
5. **No authorization system** — the HTTP API relies on local loopback
   binding, explicit `--allow-root` path confinement, and actor headers for
   attribution; it does not authenticate users.
6. **No concurrency across processes on one database** — a workspace DB is
   single-writer; concurrent API requests within one process are serialised by
   SQLite transactions.
