# clockalign — two-track audio clock drift estimation & timeline correction

A local, dependency-light backend that takes two recordings of the same event
made by independent devices, estimates how much one device's **sampling clock
drifts** relative to the other (ppm) plus the fixed start **offset**, corrects
it by resampling, and reports residuals and the time range in which the
correction is trustworthy.

It deliberately does **not** trust timestamps: drift and offset are estimated
from **known sync pulses** or **shared correlated content**, outliers are
removed with an explicit robust fit, and when the evidence is insufficient the
audio is returned **uncorrected** with a typed reason.

## What it does

1. **Media parsing** (`clockalign.media`) — PCM WAV demux (8/16/24/32-bit),
   stereo channel role assignment, nominal sample-rate sanity checks.
2. **Sync extraction**
   - `clockalign.pulses` — matched-filter detection of known sync bursts,
     carrier-mismatch-robust envelope timing, DP order-preserving pairing.
   - `clockalign.correlation` — normalized cross-correlation of shared content
     inside a bounded offset window.
3. **Robust clock model** (`clockalign.timefit`) — MSAC/RANSAC affine fit
   `t_slave = (1 + drift)·t_ref + offset`, outlier rejection, frame-drop
   discontinuity splitting, explicit "insufficient evidence" refusals.
4. **Discontinuity localization** (`clockalign.discontinuity`) — content-scan
   pinning of dropped/duplicated frame cuts, with a labelled fallback.
5. **Correction** (`clockalign.resample`) — resampled audio artifact and a
   **separate** metadata time map; dropped frames become explicit zero gaps,
   regions outside the evidence are never extrapolated.
6. **Jobs & storage** (`clockalign.jobs`, `clockalign.storage`) — SQLite job
   state machine and per-step event trail.
7. **HTTP API** (`clockalign.api`) — FastAPI endpoints with request/job id
   correlation, artifacts, and an independent validation endpoint.

> **Epistemic note (also embedded in every timeline map):** pulse/correlation
> alignment establishes *relative* time. A correlation peak is **not** proof
> of absolute wall-clock time; absolute interpretation requires an external
> anchor the backend does not invent (`external_time_anchor` flag).

## Layout

```
config/default.yaml          independent configuration (thresholds, modes)
src/clockalign/              service package (media, pulses, correlation,
                             timefit, discontinuity, resample, pipeline,
                             storage, jobs, validation, api)
tools/fixturegen/            standalone fixture oracle (imports NO core)
scripts/                     server, fixture generation, file CLI
tests/                       unit + integration + recovery tests
samples/                     generated example scenarios + truth JSON
data/                        runtime SQLite + artifacts (gitignored)
```

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 1) generate synthetic scenarios (known ppm/offset/drops/false sync)
.venv/bin/python scripts/generate_fixtures.py

# 2) run the tests
.venv/bin/python -m pytest

# 3) start the API
CLOCKALIGN_HOME=./data .venv/bin/python scripts/run_server.py
# -> http://127.0.0.1:8080  (config: config/default.yaml)
```

Environment:
- `CLOCKALIGN_CONFIG` — path to a YAML config (default `config/default.yaml`).
- `CLOCKALIGN_HOME` — directory for `jobs.db` and `artifacts/` (default `./data`).

### Run one file directly (no HTTP)

```bash
.venv/bin/python scripts/align_file.py --stereo samples/drift_offset.wav \
    --out-dir /tmp/out
# writes corrected_slave.wav, overlay.wav, timeline_map.json and prints report
```

## HTTP usage

```bash
# Submit (stereo file: ch0 reference, ch1 slave by config)
curl -s -X POST localhost:8080/api/v1/align \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-42' \
  -d '{"stereo_path":"samples/dropped_frames.wav"}'
# {"job_id":"...","request_id":"demo-42","status":"queued"}

# Poll status — carries version, config source, ordered step events, report
curl -s localhost:8080/api/v1/jobs/<job_id>

# Artifacts: corrected audio is separate from the metadata time map
curl -OJ localhost:8080/api/v1/jobs/<job_id>/artifacts/corrected_slave.wav
curl -s  localhost:8080/api/v1/jobs/<job_id>/artifacts/timeline_map.json

# Independent verification against known truth
curl -s -X POST localhost:8080/api/v1/validate -H 'Content-Type: application/json' \
  -d '{"job_id":"<job_id>","truth":{"drift_ppm":80,"offset_s":-0.12,
                                    "drop_times_s":[7.0]}}'
```

Endpoints: `GET /health` · `POST /api/v1/align` · `GET /api/v1/jobs` ·
`GET /api/v1/jobs/{id}` · `GET /api/v1/jobs/{id}/artifacts/{name}` ·
`POST /api/v1/validate`.

Every response carries `X-Request-ID`, `X-Service-Version` and
`X-Config-Source`. Logs are single-line JSON with `request_id`/`job_id`.

## Fixture scenarios

Generated into `samples/` with a companion `*.truth.json`:

| scenario | drift | offset | property exercised |
|---|---|---|---|
| `drift_offset` | 120 ppm | +250 ms | plain drift + offset recovery |
| `dropped_frames` | 80 ppm | −120 ms | 160-sample (10 ms) dropped block at t=7 s |
| `bad_sync` | 150 ppm | +180 ms | two slave-only false sync pulses must be rejected |
| `correlation` | 95 ppm | +200 ms | no pulses; content correlation path |
| `insufficient_sync` | 60 ppm | +50 ms | two pulses over uncorrelated noise → **no correction** |

The generator (`tools/fixturegen`) is intentionally standalone: it imports no
module from `clockalign`, so recovery tests compare the package against truth
the package never produced.

## Acceptance rules → where they live

| Rule | Implementation | Test |
|---|---|---|
| Estimate from pulses/correlation, not first-timestamp subtraction | `pulses.py`, `correlation.py` | `test_pulses.py`, `test_correlation.py`, `test_recovery.py::test_drift_estimate_requires_more_than_first_timestamp` |
| Robust fit; reject outliers; no evidence → no correction | `timefit.py` MSAC/RANSAC + segment split | `test_timefit.py`, `test_recovery.py::test_insufficient_evidence_is_not_corrected` |
| Resampled audio separate from metadata time map | `resample.py`, `timeline_map.json` | `test_resample.py`, `test_api.py::test_artifacts_separate_audio_and_timeline_map` |
| Report residuals and usable interval | `pipeline.py` residual report | `test_validation.py`, recovery/alignment tests |
| Known ppm/offset/drops/false-sync oracle | `tools/fixturegen` | `test_fixturegen.py`, `test_recovery.py` |

## Test commands

```bash
.venv/bin/python -m pytest                      # full suite
.venv/bin/python -m pytest tests/test_timefit.py -q
.venv/bin/python -m pytest -k recovery -q       # end-to-end recovery
```

Real captured output is kept under `docs/`:
- `docs/test_output.txt` — tail of a full `pytest` run (62 passed, 1 skipped).
- `docs/example_run.txt` — the dropped-frame scenario: recovered drift/offset,
  residual, usable interval and the localized 7.013 s / 161-sample gap.
