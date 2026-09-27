# Three-Way Structure-Preserving Text Merge Backend

A Python / FastAPI / SQLite backend that merges two independently edited
texts over a common **base** while preserving line structure exactly.
It auto-merges disjoint edits, reports explicit conflicts for everything it
cannot decide (never guessing), and lets a caller rebuild the result from
per-conflict choices. All data is synthetic and local: no production
accounts or external services are required.

---

## 1. What it does

Given `base`, `local`, `remote`:

1. Diff each side against the base with a deterministic Myers
   shortest-edit-script over whole **lines** (each line carries its own
   terminator).
2. Group edits that interact; combine them under fixed rules (below).
3. If every interaction is resolvable, return the auto-merged text.
4. Otherwise return **conflict blocks** that carry the exact three-way
   source ranges and both alternatives — no conflict markers, no guessed
   content. The caller resolves every conflict id explicitly and rebuilds.

### Module responsibilities (real separation, not a single-file script)

| Module | Responsibility |
|---|---|
| `src/merge3/textmodel.py` | Text normalization: line/terminator tokens, offsets, explicit EOL conversion (never applied implicitly) |
| `src/merge3/model.py` | Value types: `Edit`, `Region`, `ConflictBlock`, `MergeResult`, conflict categories |
| `src/merge3/diff.py` | Deterministic Myers SES, content-anchored snake recovery, edit emission |
| `src/merge3/merge.py` | The merge algorithm: clustering, interaction rules, provenance, rebuild |
| `src/merge3/storage.py` | SQLite version store: documents, content-addressed versions, merges, conflicts, resolutions |
| `src/merge3/diagnostics.py` | Request-correlated structured logs, state digests, secret redaction |
| `src/merge3/config.py` | Pydantic-settings configuration, isolated from logic |
| `src/merge3/service.py` | Orchestration: validation, engine, storage, diagnostics, failure categories |
| `src/merge3/api.py` | FastAPI HTTP layer |

Tests live in `tests/`, fixtures in `tests/fixtures.py` + `fixtures/`,
configuration in `pytest.ini` / `pyproject.toml`; the standalone checker is
`scripts/verify.py`.

---

## 2. Boundary semantics (the contract)

Offsets are character offsets, half-open (`[start, end)`). Line indexes are
0-based half-open too; a pure insertion at a boundary has
`line_start == line_end`.

1. **Disjoint edits auto-merge.** Half-open ranges that merely abut
   (`[a,b)` and `[b,c)`) are disjoint; both edits are taken.
2. **Same-point insertion.** Identical insertions at one boundary are taken
   once. Different insertions at the same point conflict
   (`same_point_insert`); choices are `local`, `remote`, `base` (insert
   nothing), `local_then_remote`, `remote_then_local`, or `custom_text`.
3. **Insertion versus a changed range.** An insertion at a boundary
   **strictly inside** the other side's replaced span conflicts
   (`insert_range`) — its anchor disappeared into replaced content, so its
   position cannot be determined. An insertion at either **edge** is
   unambiguous (line inserts are anchored "before base line k") and
   auto-merges deterministically (insert leads at the shared start, follows
   at the shared end).
4. **Delete versus modify.** One side deleting exactly (or covering) what
   the other changes is a `delete_modify` conflict; choices `local`,
   `remote`, `base`, `custom_text`.
5. **Divergent modification.** The same span changed differently is a
   `divergent_modify` conflict.
6. **Partial overlap.** Ranges that overlap without one covering the other
   are a `partial_overlap` conflict; no proportional splicing is attempted.
7. **Identical changes deduplicate**, regardless of kind.
8. **No guessing.** With any conflict the core returns no merged text and
   emits no conflict markers. Rebuild requires a valid choice for **every**
   conflict id; unknown ids, missing ids, and invalid choices are rejected
   with named error categories.
9. **Terminators are data.** `LF`, `CRLF`, `CR`, and a missing trailing
   newline are preserved exactly. The merge rewrites only what edits cover.
   EOL conversion exists as an *explicit* helper (`normalize_eol`) and is
   never invoked by the merge.

### Determinism

The Myers tie-break prefers the insert move on equal furthest diagonals,
which anchors replacements to the earliest matching occurrence inside runs
of repeated lines. Runs are identical across executions, so conflict ids
(`c1`, `c2`, … in base-position order) and outputs are stable.

---

## 3. Conflict provenance

Every conflict block contains:

* `conflict_id`, `conflict_type`;
* `base_region`, `local_region`, `remote_region` — document name, character
  `[start,end)` and line `[line_start,line_end)` ranges. Side regions use
  pre-edit anchors: a deleted span collapses to an empty point; a pure
  insert maps to the insertion boundary;
* `base_text`, `local_text`, `remote_text` — the materialized alternatives;
* `local_edit_ids`, `remote_edit_ids` — the exact edits participating;
* `allowed_resolutions` — the only choices the core will accept.

Nothing here is inferred user intent; the block is sufficient for a UI to
render a three-way view and for an audit to reproduce the decision.

---

## 4. Diagnostics & sensitive data

Each decision (accepted cluster or conflict) emits a structured record with
`request_id`, the event (`merge_started`, `merge_accepted`,
`merge_indeterminate`, `merge_rejected`, `resolution_rebuilt`, …), the key
state (spans, edit ids, conflict types, character counts, 12-char SHA-256
digests), and a human-readable `reason` saying **why** it accepted, rejected,
or could not decide.

Document text is **never** logged — only lengths, EOL counts, and digests.
Credential-shaped fields (`api_key`, `token`, `secret`, `password`,
`authorization`, …) are masked with `<redacted:len=N,sha256_12=…>`, and
free-text values are scrubbed for token / bearer / long-hex patterns. The
behavior is exercised with a fake-secret fixture and asserted in tests and
in `scripts/verify.py`.

---

## 5. HTTP API

Start locally:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-lock.txt
uvicorn merge3.api:app --app-dir src --host 127.0.0.1 --port 8000
```

* `POST /v1/merges` — `{document_id?, base_text, local_text, remote_text,
  request_id?}` → `status: auto` + `merged_text`, or `status: conflict` +
  conflict blocks + diagnostics.
* `POST /v1/merges/{merge_id}/resolve` —
  `{resolutions: {c1: {choice, text?}}}` → rebuilt text.
* `GET /v1/merges/{merge_id}` — persisted provenance, conflicts, resolutions.
* `GET /v1/documents/{id}/versions`, `GET /health`.

Error categories map to status codes: `input_invalid` 400,
`payload_too_large` 413, `not_found` 404, `resolution_invalid` 409. A merge
with conflicts is still **200** with `status:"conflict"` — indeterminacy is
a normal, represented result, not a server error.

Quick example:

```bash
curl -s localhost:8000/v1/merges -H 'content-type: application/json' -d '{
  "base_text":"a\nb\nc\n",
  "local_text":"a\nB\nc\n",
  "remote_text":"a\nb\nc\nd\n"}'
```

---

## 6. Evidence: tests and verification

Expected answers are **hand-authored literals** in `tests/fixtures.py` and
`fixtures/merge_cases.json`; they are never produced by the code under test.
`tests/reference_merge.py` is an independent textbook diff3/LCS walker with
zero shared algorithm code, used to cross-check conflict-free outputs and to
confirm the engine never flags something an independent merger trivially
resolves.

```bash
# full suite (unit + storage + end-to-end HTTP + independent cross-check)
pytest -q

# standalone, no-pytest end-to-end checker (prints every check)
python scripts/verify.py
```

Coverage of the requested scenarios:

* **moved similar paragraphs** — `move_disjoint`; move and an unrelated edit
  both survive exactly.
* **repeated lines** — `duplicate_lines_disjoint` (both edits apply) and
  `duplicate_lines_same_spot` (divergent conflict, not a silent pick).
* **same-point inserts** — conflict, identical-dedup, and different-boundary
  auto-merge; all five ordering choices reconstruct literal outputs.
* **delete/modify contention** — both orientations, each choice rebuilt to
  the exact side/base text.
* **independent-edit consistency** — applying sides in either order
  converges to the same text (cross-check test).
* **terminator fidelity** — all-CRLF, mixed LF/CRLF, and no-trailing-newline
  cases assert exact bytes, including that a missing final newline is not
  gained.
* **failure categories** — missing/unknown/illegal resolutions return named
  errors; oversize and malformed payloads return 413/4xx.
* **diagnostics** — request correlation, accept/indeterminate reasons, and
  fake-secret non-leakage are asserted.

---

## 7. Scope boundaries and explicit non-claims

These are deliberate limits, not bugs:

* **Line granularity.** All edits are line ranges. An intra-line change is
  represented as replacement of the whole line (terminator included). A
  merge that two users make to different *words of the same single line*
  reports a conflict rather than producing a word-level merge.
* **No conflict-marker output.** The API never returns `<<<<<<<` markers;
  conflicted content is returned as structured blocks and rebuilt only
  after an explicit resolution.
* **In-process engine registry.** Pending conflict engines are held in the
  service process; the inputs, conflict blocks, and resolutions are durable
  in SQLite, but calling `/resolve` after a server restart currently
  requires re-submitting the merge. This is a known boundary, not exercised
  as a passed check.
* **Single request at a time semantics.** The repository opens one SQLite
  connection (WAL); deployment under multi-worker write concurrency is not
  load-tested here.

### Checks not executed (not represented as passed)

* No performance/load benchmark was run; `MERGE3_MAX_DOCUMENT_CHARS` bounds
  inputs but throughput and memory on large corpora are **unmeasured**.
* No network/TLS or authenticated-deployment testing — the service binds
  localhost and the test client is in-process.
* No concurrency/stress run against the SQLite store.
* The independent reference merger validates conflict-free outcomes and
  conflict *presence*; it does not adjudicate the engine's specific
  sub-category labels (`insert_range` vs `partial_overlap`), which are
  asserted directly against the documented rules instead.
