# Example requests (run server first:  make serve  /  uvicorn zindex.api:app)
# Base URL default: http://127.0.0.1:8000

# 1. Health / engine version
curl -s http://127.0.0.1:8000/health | python3 -m json.tool

# 2. Create a 2D signed 16-bit-per-axis schema
curl -s -X POST http://127.0.0.1:8000/schemas \
  -H 'Content-Type: application/json' \
  -d '{"name":"points2d","dims":[
        {"name":"x","bits":16,"signed":true},
        {"name":"y","bits":16,"signed":true}]}' | python3 -m json.tool

# 3. Ingest 50k deterministic synthetic rows (uniform), chunks of 8192
curl -s -X POST http://127.0.0.1:8000/schemas/points2d/ingest_synthetic \
  -H 'Content-Type: application/json' \
  -d '{"n":50000,"shape":"uniform","seed":20260928,"capacity":8192}' | python3 -m json.tool

# 4. Ingest raw rows directly
curl -s -X POST http://127.0.0.1:8000/schemas/points2d/ingest \
  -H 'Content-Type: application/json' \
  -d '{"rows":[[0,0],[-1,1],[32767,-32768]],"capacity":8192}' | python3 -m json.tool

# 5. Range query: thin negative box, tight interval budget (candidate inflation allowed)
curl -s -X POST http://127.0.0.1:8000/schemas/points2d/query \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-query-0001' \
  -d '{"lo":[-32768,-32768],"hi":[-32000,-32000],"max_intervals":256}' | python3 -m json.tool

# 6. Same box with budget=1 to observe conservative coverage + uncertainty
curl -s -X POST http://127.0.0.1:8000/schemas/points2d/query \
  -H 'Content-Type: application/json' \
  -d '{"lo":[-100,-100],"hi":[100,100],"max_intervals":1}' | python3 -m json.tool

# 7. Globally rewrite/compact chunks (stable __row_id preserved)
curl -s -X POST http://127.0.0.1:8000/schemas/points2d/rewrite \
  -H 'Content-Type: application/json' -d '{"capacity":4096}' | python3 -m json.tool

# 8. Follow the audit trail by request id
curl -s http://127.0.0.1:8000/requests/demo-query-0001 | python3 -m json.tool

# 9. Failure-class examples
curl -s -X POST http://127.0.0.1:8000/schemas/missing/query \
  -H 'Content-Type: application/json' -d '{"lo":[0],"hi":[1]}'        # -> 404 schema_not_found
curl -s -X POST http://127.0.0.1:8000/schemas/points2d/ingest \
  -H 'Content-Type: application/json' -d '{"rows":[[99999,0]]}'      # -> 400 invalid_coordinate
curl -s -X POST http://127.0.0.1:8000/schemas/points2d/query \
  -H 'Content-Type: application/json' -d '{"lo":[40000,0],"hi":[1,0]}' # -> 400 invalid_box
