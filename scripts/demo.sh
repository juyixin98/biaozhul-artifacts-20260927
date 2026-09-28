#!/usr/bin/env bash
# End-to-end local demo: build synthetic fixtures, run the service, POST each
# archive, and print the verdict. Everything is local and synthetic.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY=.venv/bin/python
FIX=/tmp/ag-fixtures
PORT="${PORT:-8080}"
BASE="http://127.0.0.1:${PORT}"

echo "==> Generating synthetic fixtures under ${FIX}"
rm -rf "$FIX"; mkdir -p "$FIX"
PYTHONPATH=tests "$PY" - "$FIX" <<'PYEOF'
import io, os, sys, zipfile
sys.path.insert(0, "tests")
from fixtures_archive import *
out = sys.argv[1]
def w(name, data):
    with open(os.path.join(out, name), "wb") as fh:
        fh.write(data)

w("benign.zip", build_zip([
    ZipSpec("docs/readme.txt", data=b"hello archive"),
    ZipSpec("docs/sub/note.txt", data=b"nested note"),
    ZipSpec("link", kind="symlink", target=b"docs/readme.txt"),
]))
w("traversal.zip", build_zip([ZipSpec("../../tmp/evil.sh", data=b"pwn")]))
w("absolute.zip", build_zip([ZipSpec("/tmp/abs-evil", data=b"pwn")]))
w("case-collision.zip", build_zip([
    ZipSpec("Data.TXT", data=b"a"), ZipSpec("data.txt", data=b"b"),
]))
w("duplicate.zip", zip_with_duplicate_name(b"first", b"second"))
w("link-loop.zip", build_zip([
    ZipSpec("a", kind="symlink", target=b"b"),
    ZipSpec("b", kind="symlink", target=b"a"),
]))
w("link-escape.zip", build_zip([
    ZipSpec("l", kind="symlink", target=b"../../../etc/passwd"),
]))
w("dangling.zip", build_zip([
    ZipSpec("l", kind="symlink", target=b"missing-file"),
]))
w("hardlink.tar", build_tar([
    ("file", "real.txt", b"hi"), ("hardlink", "hl", "real.txt"),
]))
w("fifo.tar", build_tar([("fifo", "spooky")]))
w("declared-size.zip", zip_declared_size_mismatch())
w("bad-crc.zip", zip_with_bad_crc())
w("not-archive.dat", b"this is plain text, not an archive")

def bomb():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("bomb.bin", b"\0" * (64 * 1024 * 1024))
    return buf.getvalue()
w("zip-bomb.zip", bomb())
print("fixtures:", sorted(os.listdir(out)))
PYEOF

echo "==> Starting service on ${BASE} (fresh ./var)"
rm -rf var
PYTHONPATH=app ARCHGUARD_HOME=./var "$PY" -m archguard \
    --config config.example.json --port "$PORT" >/tmp/ag-demo-server.log 2>&1 &
SRV=$!
trap 'kill "$SRV" >/dev/null 2>&1 || true' EXIT

# Wait for readiness.
for _ in $(seq 1 50); do
  curl -s "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.2
done

echo
printf "%-20s %-9s %-26s %s\n" "FIXTURE" "HTTP" "CATEGORY" "ENTRY"
printf "%-20s %-9s %-26s %s\n" "-------" "----" "--------" "-----"
for f in benign traversal absolute case-collision duplicate link-loop \
         link-escape dangling hardlink fifo declared-size bad-crc \
         zip-bomb not-archive; do
  if [ -f "$FIX/$f.zip" ]; then arc="$f.zip"; elif [ -f "$FIX/$f.tar" ]; then arc="$f.tar"; else arc="$f.dat"; fi
  code=$(curl -s -o "$FIX/resp.json" -w "%{http_code}" \
         -F "file=@$FIX/$arc" "$BASE/api/v1/inspect")
  read -r cat entry < <("$PY" - "$FIX/resp.json" <<'PYEOF'
import json, sys
b = json.load(open(sys.argv[1]))
f = b.get("failure") or {}
print(f.get("category", "ACCEPTED"), f.get("entry") or "-")
PYEOF
)
  printf "%-20s %-9s %-26s %s\n" "$arc" "$code" "$cat" "$entry"
done

echo
echo "==> Audit hash chain:"
curl -s "$BASE/api/v1/audit/verify"; echo
echo "==> Materialized run dirs (rejected runs leave none):"
ls -1 var/runs 2>/dev/null | sed 's/^/  /'
echo "==> Spool (must be empty after finalization):"
ls -1 var/spool 2>/dev/null | sed 's/^/  /'
echo "(end of list)"
