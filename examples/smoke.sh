#!/usr/bin/env bash
# End-to-end smoke run against a local server. Set BASE to target another host.
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"
j() { python3 -m json.tool; }

echo "== 1. create resource owned by platform (keyed containers, set tags)"
curl -s -X POST "$BASE/v1/resources" -d '{
  "id":"app",
  "manager":"platform",
  "body":{"spec":{"replicas":3,"tags":["x","y"],"containers":[{"name":"web","image":"web:1","port":8080},{"name":"worker","image":"worker:1"}]}},
  "schema":{"lists":{".spec.tags":"set",".spec.containers":"keyed"},"keys":{".spec.containers":"name"}}
}' | j

echo "== 2. autoscaler adopts replicas at current value (shares the leaf)"
curl -s -X POST "$BASE/v1/resources/app/apply" \
  -d '{"manager":"autoscaler","body":{"spec":{"replicas":3}}}' | j

echo "== 3. autoscaler force-scales to 7 (platform still holds a share)"
curl -s -X POST "$BASE/v1/resources/app/apply" \
  -d '{"manager":"autoscaler","force":true,"reason":"scale-up","body":{"spec":{"replicas":7}}}' | j

echo "== 4. platform stale write replicas=11 without force -> 409 conflict"
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/v1/resources/app/apply" \
  -d '{"manager":"platform","body":{"spec":{"replicas":11}}}'

echo "== 5. netpol adds a set tag and changes web.port via force"
curl -s -X POST "$BASE/v1/resources/app/apply" \
  -d '{"manager":"netpol","body":{"spec":{"tags":["z"]}}}' | j
curl -s -X POST "$BASE/v1/resources/app/apply" \
  -d '{"manager":"netpol","force":true,"body":{"spec":{"containers":[{"name":"web","port":9090}]}}}' | j

echo "== 6. platform deletes worker; carries current foreign values so they survive"
curl -s -X POST "$BASE/v1/resources/app/apply" -d '{
  "manager":"platform",
  "body":{"spec":{"replicas":7,"tags":["x","y"],"containers":[{"name":"web","image":"web:1","port":9090}]}}
}' | j

echo "== 7. final resource, owners, history"
curl -s "$BASE/v1/resources/app" | j
curl -s "$BASE/v1/resources/app/owners" | j
curl -s "$BASE/v1/resources/app/history?limit=5" | j
