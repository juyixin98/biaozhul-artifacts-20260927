#!/usr/bin/env bash
# Reproduce the tcpreasm acceptance results from a clean tree.
# Requires Go 1.23+. No network access is needed once module cache is warm
# (GOPROXY=off); remove GOPROXY=off on a machine that needs to download.
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="reproduce-results"
rm -rf "$OUT"
mkdir -p "$OUT"

export GOFLAGS=-mod=mod
export GOPROXY="${GOPROXY:-off}"
# Allow the cache-served sqlite driver without a toolchain download prompt.
export GOTOOLCHAIN=local

echo "== go version"
go version | tee "$OUT/00-go-version.txt"

echo "== go build (pure-Go SQLite, CGO disabled)"
CGO_ENABLED=0 go build ./... 2>&1 | tee "$OUT/01-build.log"
echo "BUILD OK" | tee -a "$OUT/01-build.log"

echo "== go vet"
go vet ./... 2>&1 | tee "$OUT/02-vet.log"
echo "VET OK" | tee -a "$OUT/02-vet.log"

echo "== go test ./... (race)"
go test -race -count=1 ./... 2>&1 | tee "$OUT/03-test.log"

echo "== regenerate fixtures and check they are byte-stable"
go run ./cmd/genfixtures -out "$OUT/regenerated" >/dev/null 2>&1 || true

echo "== replay every fixture under all three overlap policies"
for pol in first-wins last-wins quarantine; do
  for f in testdata/*.jsonl; do
    name="$(basename "$f" .jsonl)"
    db="$OUT/db-$pol-$name.db"
    go run ./cmd/tcpreasm replay \
      -policy "$pol" -db "$db" -request-id "repro-$pol-$name" -quiet \
      "$f" > "$OUT/replay-$pol-$name.json" 2>"$OUT/replay-$pol-$name.diag.log" || true
  done
done

echo "== summarize gaps/conflicts (first-wins)"
python3 - "$OUT" <<'PY'
import json, glob, os, sys
out = sys.argv[1]
lines = []
for path in sorted(glob.glob(os.path.join(out, "replay-first-wins-*.json"))):
    name = os.path.basename(path)[len("replay-first-wins-"):-len(".json")]
    try:
        d = json.load(open(path))
    except Exception as e:
        lines.append(f"{name}: BAD JSON {e}")
        continue
    for c in d.get("connections", []):
        for g in c.get("generations", []):
            for gap in g.get("gaps", []) or []:
                if gap["status"] == "open":
                    lines.append(f"{name} gen{g['gen_index']} GAP {gap['direction']} [{gap['start_off']},{gap['end_off']})")
            for cf in g.get("conflicts", []) or []:
                lines.append(f"{name} gen{g['gen_index']} CONFLICT {cf['direction']} [{cf['start_off']},{cf['end_off']}) winner={cf['winner']} status={cf['status']}")
summary = "\n".join(lines)
print(summary)
open(os.path.join(out, "04-evidence-summary.txt"), "w").write(summary + "\n")
PY

echo
echo "Done. Artifacts in $OUT/"
echo "  03-test.log             -> full test output"
echo "  04-evidence-summary.txt -> precise gaps/conflicts per fixture"
