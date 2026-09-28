# Offline Secret-Candidate Scanner

Offline scanning of **code repository snapshots** for secret *candidates*,
combining versioned structural rules with Shannon-entropy thresholds. The
tool never validates credentials over a network and never labels a hit a
confirmed leak — every result is a candidate for human review.

Built with Python 3.12 / FastAPI / SQLite / `cryptography`. Everything runs
locally on synthetic fixtures; no production accounts or real business data
are required.

## What it guarantees

1. **Versioned scope and rules; fingerprint-bound baselines.** Rule packs and
   scope/ignore packs are independently versioned TOML files; every report
   records the exact version *and* a SHA-256 of the pack bytes. Baseline
   exemptions bind the **secret content** via a keyed HMAC fingerprint, not
   the file name — moving an exempted file keeps the exemption, changing one
   character of the secret drops it.
2. **Masks and positions only.** Persistence, HTTP responses and logs store a
   mask (`ghp_1eAo…Eika`) and an HMAC-SHA256 fingerprint — never the full
   secret. A redacting log filter scrubs raw values even from accidental log
   statements.
3. **Text vs binary; explicit "not scanned".** NUL/non-UTF-8 content is
   scanned through printable-ASCII runs (positions reported as byte offsets);
   files at/above the size limit are **not opened** and are listed separately
   as unscanned, along with unreadable files and un-followed symlinks.
4. **Candidates, not verdicts.** Each hit carries a `confidence`
   (`low|medium|high`) and a mandatory disclaimer; nothing is asserted to be
   a live/valid credential. Lifecycle classifications (`known_fixed`,
   `moved`, `uncertain_removal`) describe *what happened to candidate content
   between two offline snapshots* — nothing more.

## Module layout (split by responsibility)

| Module | Responsibility |
|---|---|
| `secretscan/config.py` | Rule/evidence parsing; versioned TOML packs, glob translation, settings |
| `secretscan/security.py` | **Safety kernel**: `Secret` wrapper, masking, HMAC fingerprints, log redaction |
| `secretscan/entropy.py` | Shannon entropy primitive |
| `secretscan/media.py` | Text/binary classification; printable-run extraction |
| `secretscan/scanner.py` | Pure scan engine: snapshot → inventory + candidates |
| `secretscan/baseline.py` | Content-fingerprint-bound exemptions |
| `secretscan/state.py` | Per-workspace isolated SQLite state + schema |
| `secretscan/audit.py` | Structured JSON audit log, request/actor identity |
| `secretscan/service.py` | Orchestration, lifecycle diff, report assembly |
| `secretscan/api.py` | Local read-only audit HTTP interface (FastAPI) |
| `secretscan/cli.py` / `serve.py` | Command-line / loopback HTTP entrypoints |
| `tools/make_baseline.py` | Independent (stdlib-only) fingerprint generator |
| `scripts/verify.py` / `verify.sh` | Acceptance scenarios + full verification |

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Scan the synthetic fixture repository
python -m secretscan.cli scan fixtures/repo \
    --baseline fixtures/baseline.toml --workspace .secretscan/demo.db
```

Serve the local audit API (loopback only, with an explicit allow-listed root):

```bash
python -m secretscan.serve --allow-root "$PWD/fixtures" --port 8099
# then, in another shell:
curl -s -X POST http://127.0.0.1:8099/scans \
    -H 'Content-Type: application/json' \
    -H 'X-Request-Id: demo-1' -H 'X-Actor-Id: alice' \
    -d '{"root": "'"$PWD"'/fixtures/repo"}' | python -m json.tool
```

## Configuration

* `config/rules/default.toml` — structural rules. A hit requires BOTH the
  regex structure AND the rule's `min_entropy`; entropy alone never produces a
  candidate. Keyword context, length gates and placeholder denylists are
  per-rule.
* `config/scopes/default.toml` — ignore patterns (gitignore-style globs),
  `max_file_bytes`, binary run length.
* `SECRETSCAN_PEPPER` — HMAC pepper for content fingerprints. The checked-in
  default is a **public development value**; real deployments must override it
  (baselines embed the pepper id and refuse to load under a different pepper).

## Reports

The report JSON groups findings by lifecycle state and has dedicated sections:

* `findings.new|open|moved|known_fixed|uncertain_removal|baseline_exempt`
* `unscanned` — files not examined, with `reason_code` (`file_too_large`,
  `symlink_not_followed`)
* `failures` — unreadable files (`file_unreadable`)
* `ignored` — paths excluded on purpose by scope rules
* `uncertainties` — conclusions that cannot be stated as fixes (e.g. the old
  path vanished or is now ignored)
* `versions` — exact rule/scope fingerprints + pepper id + baseline path

## Tests and verification

```bash
./scripts/verify.sh          # venv + pinned deps + pytest + e2e scenarios
# or separately:
pytest -q                    # 100+ tests with concrete assertions
python scripts/verify.py     # moved/deleted/fixed/oversize/redaction e2e
```

The suite uses only **synthetic fake credentials** (AWS-documented example
values, fabricated GitHub/Slack-shaped tokens, a hand-made PEM body). It
asserts exact rule ids, positions, masks, independently computed HMAC
fingerprints, lifecycle states and failure reason codes — not merely that
endpoints respond. Reference fingerprints come from
`tools/make_baseline.py`, a separate stdlib-only implementation that imports
none of the code under test.

## Boundaries

See [`docs/BOUNDARIES.md`](docs/BOUNDARIES.md) for the precise semantics,
including what is deliberately **not** checked. Checks that cannot be
performed offline are listed there and in verification output — never reported
as passed.
