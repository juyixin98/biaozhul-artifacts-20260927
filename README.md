# Local MPEG-TS Analysis Backend

A self-contained local backend that parses **188-byte MPEG-2 Transport Stream**
packets (ISO/IEC 13818-1), recovers from sync loss, checks per-PID continuity
counters, reassembles PAT/PMT tables with CRC verification, performs atomic
table-version switches, and does *constrained* PES reassembly with gap
detection. Built with **Python + FastAPI + NumPy + SQLite**; every input is a
locally synthesized fixture — no external services or real broadcast data.

## Layout

```
app/
  config.py            environment-driven Settings (MTSA_* env vars)
  core/
    sync.py            188-byte framing + bounded sync-recovery scanner
    continuity.py      per-PID continuity-counter checker
    psi.py             PSI section reassembly, CRC32/MPEG-2, PAT/PMT,
                       atomic version switching
    pes.py             constrained PES reassembly + PTS/DTS decode
    timing.py          PCR / PTS-DTS signal kernel (NumPy reductions)
    diagnostics.py     Finding model (code, severity, disposition, state)
    analyzer.py        orchestration -> AnalysisReport
  jobs/manager.py      SQLite (WAL) async job store + background worker
  api/
    app.py             FastAPI factory + request-id / error middleware
    routes.py          /health, /validate, /jobs, /jobs/upload, job status
tools/tsbuilder.py     INDEPENDENT synthetic stream builder (imports nothing
                       from app.core) + independent CRC implementation
scripts/
  make_fixtures.py     writes *.ts + *.expected.json ground-truth manifests
  verify.py            standalone verifier (fixtures or arbitrary .ts files)
tests/                 60 pytest cases across all kernels + API
```

The reference answers in `tests/` and `scripts/verify.py` come from the
builder's by-construction ledger and the `*.expected.json` manifests, **not**
from the code under test.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # pinned direct deps
# or, for the fully resolved transitive set:
pip install -r requirements-lock.txt

# regenerate synthetic fixtures (also done automatically by the test run)
python -m scripts.make_fixtures

# run the test suite
python -m pytest                          # 60 passed

# standalone verification (prints per-fixture assertions + what is NOT run)
python scripts/verify.py

# analyze an arbitrary local file
python scripts/verify.py path/to/stream.ts

# run the API
uvicorn app.api.app:app --port 8000
```

## HTTP interface

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | liveness + request id |
| `POST` | `/api/v1/validate` | synchronous analysis of a raw `video/mp2t` body |
| `POST` | `/api/v1/jobs` | queue an analysis job (raw body) → `202` + `job_id` |
| `POST` | `/api/v1/jobs/upload` | queue a job from a multipart `file` |
| `GET`  | `/api/v1/jobs/{id}` | job status + report when `done` |
| `GET`  | `/api/v1/jobs` | recent jobs |

Every request/response carries `X-Request-ID` (client-supplied via the
`X-Request-ID` header, or generated). Oversized bodies return `413` while
streaming; empty bodies return `400`. Uploaded filenames are masked
(`redact_label`) before storage/logging; analyzed bytes are never logged.

## Contract semantics (the important boundary details)

### Sync recovery — bounded scan
After losing lock the scanner does **not** accept the first `0x47` it sees: it
requires sync bytes at the 188-byte packet period for two confirmations, and
searches only within `max_scan_bytes` (default 1880). A failure to re-lock is
reported as `trailing_bytes`/`no_sync` with the byte range, never silently
truncated.

### Continuity counters — checked independently per PID
* The counter increments **only on packets carrying a payload**. An
  adaptation-only packet repeats the previous CC; a changed CC there is
  `adaptation_only_cc_mismatch` (rejected).
* An exact repeat (same CC, identical raw packet) is `duplicate_packet`
  (accepted). Same CC with **different bytes** is the distinct failure
  `cc_repeat_payload_without_duplicate_bit` (rejected).
* A counter jump of 2..15 with no flag is `cc_gap` (rejected), carrying
  `expected_cc`, observed `cc`, and `missing_estimate`.
* The adaptation-field **discontinuity indicator is a separate fact from a
  real loss**: a jump accompanied by the indicator is
  `signaled_discontinuity` (accepted, counter baseline restarts), while the
  indicator on an otherwise continuous counter is
  `discontinuity_flag_without_gap` (accepted, informational).
* The null PID `0x1FFF` is not checked.

### PSI tables — atomic updates
Sections are reassembled across packets honoring `pointer_field` and TS
stuffing, then verified with CRC32/MPEG-2. A section with a bad CRC is
rejected (`section_crc_error`, expected vs actual CRC) and **cannot mutate the
live program map**. A PAT/PMT version replaces the previous map in one
operation only after all sections of the new version are present and verified
(`table_version_switch` audit finding). PMT PIDs are learned from the PAT and
elementary PIDs from the PMTs — there is no fixed-PID shortcut; payload on a
PID not referenced by any seen PAT/PMT is `unknown_pid_payload`
(undetermined).

### Constrained PES reassembly
Only PMT-declared elementary PIDs are reassembled; each PES is capped by
`max_pes_bytes` (`pes_oversize`). A length-bounded PES self-closes at its
declared `packet_length` so TS stuffing and the next PES are not swallowed.
An unsignaled CC gap inside a PES marks it corrupt (`pes_gap`); a signaled
discontinuity discards the in-flight fragment without counting a gap; exact
duplicate fragments are not concatenated twice. PTS/DTS are decoded with
marker-bit checks.

### Diagnostics
Each `Finding` is one of `accepted` / `rejected` / `undetermined` and carries
its `code`, packet index, PID, and the key state motivating the verdict
(expected vs observed CC, the two CRCs, skipped byte range, buffered byte
count). The first packet of a PID is explicitly `undetermined`
(`cc_baseline`) rather than guessed.

## Fixture scenarios

`python -m scripts.make_fixtures` produces, with independent manifests:

* `baseline` — valid PAT + PMT, two video PES + one audio PES
* `duplicate` — exact repeat packet, accepted and not double-assembled
* `gap` — 3 packets lost inside a PES; specific CC gap + PES gap
* `adaptation_signaled` — adaptation-only non-increment + DI vs real loss
* `cross_packet_pmt` — a >183-byte PMT spanning TS packets
* `bad_crc` — corrupted PMT CRC: map stays empty, video PID is "unknown"
* `version_switch` — atomic PAT v1→v2 and PMT v1→v2
* `resync` — garbage run with bounded recovery, state reset, map retained

## Known boundaries / semantics not assumed

* **Header-straddle heuristic**: if a section's 3-byte header is split so
  that 1–2 real bytes are followed by packet-end `0xFF` stuffing, the parser
  strips the stuffing. The theoretically ambiguous case — a
  `section_length` whose *low byte is 0xFF* (255/511/767) combined with that
  exact fragmentation — is treated as stuffing. Standard encoders start
  sections with ≥3 header bytes available, so this does not occur in
  conformant streams.
* **PCR wrap**: PCR backwards-detection uses raw deltas; a full 2^42 PCR
  wrap (~4.8 h) is normalized only for interval statistics. Streams that long
  are out of scope for the synthetic tests.
* **Scrambling**: packets with `transport_scrambling_control != 0` are counted
  but their payloads are not parsed (no descrambler in scope).
* Checks that cannot be executed here (real broadcast capture, hardware PCR
  jitter against a physical demodulator) are listed explicitly by
  `scripts/verify.py` and are **never** reported as passed.
