#!/usr/bin/env bash
# Annotated curl walkthrough. Start the three services first:
#   ./bin/apiserver    -config configs/apiserver.json
#   ./bin/actualserver -config configs/actualserver.json
#   ./bin/controller   -config configs/controller.json
set -euo pipefail
B=http://127.0.0.1:18081
A=http://127.0.0.1:18082
D=http://127.0.0.1:18083
AUTH=local-dev-secret
j() { python3 -m json.tool; }

echo "### create"
curl -s -X POST "$B/api/v1/namespaces/demo/resources" -H 'Content-Type: application/json' \
  -d '{"name":"walk","spec":{"replicas":2,"image":"w:1","secret":"s3cr3t"}}' | j

sleep 1
echo "### get (secret redacted for unprivileged reader)"
curl -s "$B/api/v1/namespaces/demo/resources/walk" | j
echo "### get with controller credential (raw spec)"
curl -s -H "X-Controller-Auth: $AUTH" "$B/api/v1/namespaces/demo/resources/walk" | j

echo "### physical truth"
curl -s "$A/admin/resources" | j

echo "### update with optimistic concurrency"
RV=$(curl -s "$B/api/v1/namespaces/demo/resources/walk" | python3 -c 'import sys,json;print(json.load(sys.stdin)["resourceVersion"])')
curl -s -X PUT "$B/api/v1/namespaces/demo/resources/walk" -H 'Content-Type: application/json' \
  -H "If-Match: $RV" -d '{"spec":{"replicas":3,"image":"w:1","secret":"s3cr3t"}}' | j

sleep 1
echo "### stale If-Match is rejected 409"
curl -s -i -X PUT "$B/api/v1/namespaces/demo/resources/walk" -H 'Content-Type: application/json' \
  -H "If-Match: $RV" -d '{"spec":{"replicas":99}}' | head -8

UID=$(curl -s "$B/api/v1/namespaces/demo/resources/walk" | python3 -c 'import sys,json;print(json.load(sys.stdin)["uid"])')

echo "### inject a failing external delete, then delete"
curl -s -X POST "$A/admin/faults" -H 'Content-Type: application/json' \
  -d "{\"ownerUID\":\"$UID\",\"fault\":\"delete-failed\"}" | j
curl -s -X DELETE "$B/api/v1/namespaces/demo/resources/walk" | j
sleep 1
echo "### still terminating, physical row kept"
curl -s "$A/admin/resources" | j

echo "### heal, wait for purge"
curl -s -X DELETE "$A/admin/faults/$UID" -i | head -1
sleep 2
curl -s -i "$B/api/v1/namespaces/demo/resources/walk" | head -1
echo "### ledger"
curl -s "$D/diagnostics/ledger/$UID" | j
