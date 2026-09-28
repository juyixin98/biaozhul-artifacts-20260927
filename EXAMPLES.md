# Example requests

All examples assume the API is running locally:

```bash
bash setup.sh           # one-time: venv + locked deps
bash run.sh serve       # starts on 127.0.0.1:8080
```

## 1. Function selector (mature Keccak backend)

```bash
curl -s 127.0.0.1:8080/abi/selector \
  -H 'content-type: application/json' \
  -d '{"name":"transfer","types":["address","uint256"]}'
# {"selector":"0xa9059cbb","signature":"transfer(address,uint256)"}
```

## 2. Encode integers / bytes / arrays / tuples

```bash
curl -s 127.0.0.1:8080/abi/encode \
  -H 'content-type: application/json' \
  -d '{
    "types": ["uint256","bytes","int256[]","(string,uint256)","bytes[]"],
    "values": [
      42,
      "0xcafe",
      [-1, -2, 3],
      ["nested tuple", 9],
      ["", "0x0102", ""]
    ]
  }'
```

Response:

```json
{ "data": "0x00000000...", "length": 416 }
```

## 3. Decode the same blob

```bash
curl -s 127.0.0.1:8080/abi/decode \
  -H 'content-type: application/json' \
  -d '{"types":["uint256","bytes","int256[]","(string,uint256)","bytes[]"],
       "data":"0x00000000..."}'
```

`bytes`/`address` come back as `0x`-hex; integers are JSON numbers; tuples and
arrays are JSON arrays.

## 4. Encode a full contract call (selector + ABI args)

```bash
curl -s 127.0.0.1:8080/abi/encode-call \
  -H 'content-type: application/json' \
  -d '{"name":"transfer","types":["address","uint256"],
       "values":["0x00000000000000000000000000000000000000ab","1000000000000000000"]}'
```

## 5. Strict failure categories (no generic "success")

A hostile blob whose offset points into the head is rejected with the specific
code `offset_out_of_bounds` (HTTP 400), not a vague error:

```bash
curl -s 127.0.0.1:8080/abi/decode \
  -H 'content-type: application/json' \
  -d '{"types":["bytes"],
       "data":"0x0000000000000000000000000000000000000000000000000000000000000000"}'
# {"error":{"code":"offset_out_of_bounds","message":"offset 0 points into head (head_size=32)"}}
```

A huge declared length is rejected before allocation (`length_too_large`):

```bash
curl -s 127.0.0.1:8080/abi/decode \
  -H 'content-type: application/json' \
  -d '{"types":["bytes"],
       "data":"0x0000000000000000000000000000000000000000000000000000000000000020ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}'
# {"error":{"code":"length_too_large", ...}}
```

## 6. Offline replay and indexed queries

```bash
bash run.sh replay          # deterministic 9-tx scenario, prints per-tx verdicts

curl -s 127.0.0.1:8080/replay        # run again via HTTP, returns the report
curl -s 127.0.0.1:8080/runs          # replay run history
curl -s 127.0.0.1:8080/transactions  # every tx incl. failed ones with error_code
curl -s 127.0.0.1:8080/accounts      # state snapshot
curl -s 127.0.0.1:8080/events        # Transfer/Approval events
```

## 7. Library use (Python)

```python
from abibackend import abi

blob = abi.encode(["uint256", "bytes", "(int256,string[])"],
                  [7, b"hi", (-3, ["a", ""])])
print(blob.hex())
print(abi.decode(["uint256", "bytes", "(int256,string[])"], blob))
# (7, b'hi', (-3, ('a', '')))

sel = abi.function_selector("transferFrom", ["address", "address", "uint256"])
assert sel.hex() == "23b872dd"
```

Every response carries an `x-run-id` header; send your own to correlate logs:
`-H 'x-run-id: my-run-123'`.
