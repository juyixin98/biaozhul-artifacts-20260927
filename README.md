# Offline RTP Jitter Buffer & Playout Backend

A multi-module Python backend that takes a **synthetic, locally generated**
trace of RTP audio packets, runs it through a jitter buffer, and produces an
explainable playout plan. No production accounts, network calls, or real
call data are involved — every packet comes from a fixture or an uploaded
JSON trace.

Stack: **Python 3.12 · FastAPI · NumPy · SQLite · pytest**.

---

## 1. What it does (behaviour contract)

1. **Sequence numbers and timestamps are expanded by their bit width.**
   Raw 16-bit seq numbers and 32-bit RTP timestamps are extended to unbounded
   integers using modular arithmetic, so a stream crossing `65535→0` or
   `2³²→0` is handled transparently. An **SSRC change creates a new,
   independent session** (counters restart at 0 and are never cross-counted
   as duplicates).
2. **Late and duplicate packets are handled differently, and a packet can
   never be reinserted after its slot has played.**
   - `DUPLICATE` — same extended seq already buffered; first copy kept.
   - `REORDERED` — packet accepted behind the all-time high-water seq.
   - `LATE_AFTER_PLAYOUT` — arrived after that slot was played or declared a
     gap; discarded, never spliced back.
   - `BUFFER_FULL` — the bounded queue (`max_buffer_packets`) was full.
3. **The adaptive delay formula and its bounds are explicit**
   (`app/config.py`):

   ```
   q = transit − min_transit                 (relative transit per packet)
   J ← J + (|qₙ − qₙ₋₁| − J) / 16           (RFC 3550 EWMA jitter)
   d = clip(K · J + margin, min_delay, max_delay)
   ```

   Defaults: `K = 8`, `margin = 2 ms`, `min_delay = 20 ms`,
   `max_delay = 120 ms`. The delay is recomputed at the start of each
   talkspurt and frozen for that run. A fixed `20 ms` baseline is run over
   the identical trace for comparison.
4. **Missing packets are explicit gaps, never fake audio.** A missing slot is
   emitted at its deadline as a `GAP` item with `audio=None` and a declared
   `gap_length_samples`. No samples are synthesised.

Scheduled playout times are **strictly monotonic** and exactly one
`frame_ms` (10 ms @ 8 kHz / 80 samples) apart; delay adaptation can never
rewind the output clock, including across clock drift and pause/restart.

---

## 2. Project layout

```
app/
  config.py        # all delay bounds / clock params / env overrides
  time_kernel.py   # modular seq/ts arithmetic, clock-drift model (pure)
  media.py         # RTP header parser + 8-bit PCM decode, reason-coded errors
  jitter.py        # THE CORE: extension, adaptive delay, gaps, discard rules
  sessions.py      # per-SSRC session registry (SSRC change => new session)
  engine.py        # offline replay (arrival-order ingest + virtual-clock drain)
  analysis.py      # comparison summary, failure-category glossary, uncertainty
  store.py         # SQLite job/run/event persistence
  main.py          # FastAPI validation service
fixtures/          # independent reference trace generators (ground truth)
tests/             # 59 tests: arithmetic, parser, core, fixtures, HTTP
demo.py            # local CLI demo (adaptive vs fixed)
requirements.txt
```

The core mechanisms are real policy code, not hard-coded demos: fixtures only
describe *what the network did*; all classifications and timings are produced
by `app/jitter.py`.

---

## 3. Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Run the tests

```bash
source .venv/bin/activate
python -m pytest
```

The suite asserts concrete outcomes and failure categories (exact gap seqs,
duplicate counts, wraparound extension, monotonicity, bounded buffer, parse
reason codes), and the fixtures carry independent ground truth
(`lost_seqs`, `duplicated_seqs`, raw values) so tests are not the core
verifying itself.

## 5. Run the local demo

```bash
python demo.py                 # table for every fixture
python demo.py ramp_jitter     # detailed per-step trace for one fixture
python demo.py burst_reorder --json
```

Fixtures: `burst_reorder`, `ramp_jitter`, `clock_drift`, `wraparound`,
`pause_restart`, `ssrc_switch`, `malformed`.

On `ramp_jitter` the adaptive buffer widens to the peak and reports far fewer
gaps than the fixed baseline (typical result: **1 gap vs 9**).

## 6. Run the service

```bash
uvicorn app.main:app --port 8000
# OpenAPI UI: http://127.0.0.1:8000/docs
```

Endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + version |
| GET | `/api/fixtures` | list scenarios and their ground truth |
| POST | `/api/analyze` | `{"fixture": "ramp_jitter"}` → adaptive + fixed verdict |
| POST | `/api/analyze/trace` | upload your own packets (optional config overrides) |
| GET | `/api/jobs` | list persisted jobs |
| GET | `/api/jobs/{id}` | job status + correlated event log |
| GET | `/api/jobs/{id}/runs/{adaptive|fixed}` | full stored run |

Every response carries `version` and `request_id`. Send
`x-request-id: …` to supply your own correlation id; logs, the job row, and
the event log all carry it.

Example:

```bash
curl -s -X POST localhost:8000/api/analyze \
  -H 'content-type: application/json' \
  -H 'x-request-id: demo-123' \
  -d '{"fixture":"ramp_jitter"}'
```

Uploaded trace body:

```json
{
  "packets": [
    {"seq": 0, "timestamp": 0,   "ssrc": 7, "arrival_ms": 0,  "payload_hex": "8080"},
    {"seq": 2, "timestamp": 160, "ssrc": 7, "arrival_ms": 21, "payload_hex": "8080"}
  ],
  "config": {"min_delay_ms": 20, "max_delay_ms": 120}
}
```
Here seq 1 is missing and is reported as one explicit `GAP`.

---

## 7. Configuration

All constants live in `app/config.py` and can be overridden by environment
variables:

| Field | Env var | Default |
|---|---|---|
| clock rate (Hz) | `RTP_CLOCK_RATE` | 8000 |
| samples/packet | `RTP_SAMPLES_PER_PACKET` | 80 |
| min delay (ms) | `JB_MIN_DELAY_MS` | 20 |
| max delay (ms) | `JB_MAX_DELAY_MS` | 120 |
| safety margin (ms) | `JB_SAFETY_MARGIN_MS` | 2 |
| jitter multiplier K | `JB_JITTER_K` | 8 |
| buffer capacity (packets) | `JB_MAX_PACKETS` | 256 |
| fixed baseline delay (ms) | `JB_FIXED_DELAY_MS` | 20 |
| SQLite path | `JB_DB_PATH` | `jobs.db` |
| late/hole threshold (packets) | — | 2 |

`late_packet_threshold` (default 2): a missing head is confirmed as a real
hole once the highest seq seen is that many packets ahead **and** its playout
deadline has elapsed. A value ≥ 2 means ordinary adjacent reordering
(n arriving just after n+1) is still rescued, while a genuine burst loss or a
very late packet is declared on time.

---

## 8. Error semantics

Failures are reported with **stable machine-readable codes**, never only as
free text:

| Code | Meaning | HTTP |
|---|---|---|
| `TRUNCATED_HEADER` | datagram shorter than 12 bytes | recorded as parse error |
| `BAD_VERSION` | RTP version ≠ 2 | recorded as parse error |
| `TRUNCATED_EXTENSION` | extension length exceeds packet | recorded as parse error |
| `BAD_PADDING` | padding count missing/oversized | recorded as parse error |
| `DUPLICATE` | seq already buffered | ingest classification |
| `REORDERED` | accepted below high-water seq | ingest classification |
| `LATE_AFTER_PLAYOUT` | slot already played/gapped | ingest classification |
| `BUFFER_FULL` | bounded capacity reached | ingest classification |
| `UNKNOWN_FIXTURE` | unknown `/api/analyze` name | 404 |
| `JOB_NOT_FOUND` / `RUN_NOT_FOUND` | unknown lookup id | 404 |
| `UNKNOWN_MODE` | run mode ≈ adaptive/fixed | 404 |
| `UNKNOWN_CONFIG_KEYS` | unsupported trace config keys | 422 |
| `INTERNAL_ERROR` | unexpected server-side failure | 500 |

**Discard categories and uncertain conclusions are listed separately** in the
verdict (`drop_categories`, `parse_errors`, and an `uncertainty` array) and a
`failure_category_glossary` explains each in plain language. Uncertainty is a
first-class output — e.g. *"3 packet(s) failed RTP parsing and were excluded
from the session"* or *"N accepted packet(s) never reached a playout
deadline"* — rather than being silently collapsed into pass/fail.

---

## 9. Reproducing a specific finding

```bash
# adaptive vs fixed on a jitter ramp, machine readable
python demo.py ramp_jitter --json | jq '.comparison'

# persistent, correlated run via HTTP
curl -s -X POST localhost:8000/api/analyze \
  -H 'x-request-id: repro-42' -H 'content-type: application/json' \
  -d '{"fixture":"burst_reorder"}' | tee result.json
JOB=$(jq -r .job_id result.json)
curl -s localhost:8000/api/jobs/$JOB | jq '.job.events'
```

## 10. Notes on fidelity

This is an **offline, virtual-clock** replay: packets are processed in
declared arrival order and drained at each distinct arrival time, exactly as
an online receiver would, but time is simulated so traces are deterministic
and reproducible. Payload handling is limited to 8-bit PCM (PCMU-style
octets) decoded to float32 mono; the buffering and scheduling mechanics are
codec-agnostic and operate on seq/timestamp/arrival metadata.
