# netsem — offline first-match network-rule semantic analyzer

`netsem` analyzes ordered network access-control rules and detects, **by exact
set geometry rather than text comparison**, rules that cannot behave as
intended under **first-match** semantics. It is fully offline: all data is
synthetic/local, the only external dependency is a pure-Go SQLite library, and
no production account or real traffic is involved.

A rule matches on five axes:

| axis | domain |
|---|---|
| protocol | IP protocol number, 0..255 |
| source address | IPv4 **or** IPv6 CIDR (families never mixed) |
| destination address | IPv4 **or** IPv6 CIDR |
| source port | interval, 0..65535 |
| destination port | interval, 0..65535 |

## What it detects

Evaluation is strict first-match: a packet takes the **first** rule whose
region contains it; if none matches, the `default_action` applies. For each
rule the analyzer computes the region it *effectively* decides
(`region ⧵ union(earlier regions)`) using exact products-of-interval set
difference (`internal/netmodel`), independently for IPv4 and IPv6.

- **`fully_shadowed`** — the rule decides zero packets; every packet in its
  region is already decided by an earlier rule. It carries a concrete
  *witness packet* and the list of earlier rules responsible.
- **`partially_shadowed`** — the rule still decides some packets, but part of
  its region is taken earlier. It carries a witness for the lost part **and**
  the exact *covered partition* (disjoint products + packet counts) the rule
  still governs.
- **`redundant`** — deleting the rule changes **no** packet's decision. This
  is decided by re-simulating after removal: every packet the rule decides
  must be decided identically either by the first later rule that matches it
  or by the default action. The default action therefore participates
  directly (a `deny` rule under default-`deny`, or an `allow` that the
  default already grants with no intervening carve-out, is redundant).

Notes on the taxonomy:

- Shadowing ignores the earlier rule's *action* — a packet already decided by
  an earlier rule never reaches a later one, whether the verdict agrees or
  not. Redundancy is the action-aware concept.
- A rule can be `redundant` without being shadowed, and a fully shadowed rule
  is classified as shadowed (it decides nothing) rather than also redundant.
- Per-rule redundancy is **single-deletion** semantics: a rule is redundant
  if removing *that one rule* leaves the function unchanged. A pair of
  identical rules can therefore each be individually redundant even though
  deleting both would change behavior; the deletion-invariance test removes
  exactly the flagged rule and re-simulates.

## Protocol and family semantics

- IPv4 and IPv6 are compiled and analyzed as separate spaces; an IPv4 rule
  can never shadow an IPv6 rule. A rule with no address constraint projects
  into both families (emitting one finding per family where applicable).
- Named protocols: `tcp` (6), `udp` (17), `sctp` (132) carry ports;
  `icmp` (1, IPv4 only) and `icmpv6` (58, IPv6 only) do not.
- `any` spans the whole 0..255 protocol domain (so genuinely unknown wire
  protocols are covered). With a restricted port range, the port constraint
  applies only to port-bearing protocols; other protocols match regardless
  of port fields. This subtlety is surfaced as a **note**.
- A bare decimal protocol (e.g. `99`) with no catalog name is **accepted and
  matched**, but flagged uncertain (`notes` on ingest, `uncertainties` on
  evaluation) rather than silently guessed at.
- Non-port protocols collapse the port axes to `{0}` in the geometric model;
  giving `icmp` a non-wildcard port is a hard `port_not_applicable` error.

## Error semantics (stable categories)

Configuration problems never crash analysis; they are returned as structured
`parse_errors` with machine-readable categories and the config is **not**
activated (evaluation then answers `409 no_config`):

| category | meaning |
|---|---|
| `invalid_config` | body is not valid JSON, missing protocol, etc. |
| `unknown_protocol` | token is neither a known name nor 0..255 |
| `invalid_cidr` | malformed CIDR/address |
| `invalid_port` | unparsable / out-of-range / inverted port interval |
| `mixed_address_family` | one rule mixes v4 and v6 addresses, or lists CIDRs of both families |
| `protocol_family_mismatch` | e.g. `icmp` paired with an IPv6 CIDR |
| `port_not_applicable` | ports constrained on a non-port protocol |
| `invalid_action` | action/default is neither `allow` nor `deny` |
| `duplicate_rule_id` | two rules share an id |

All errors found in one pass are returned together (a later error does not
hide earlier ones). Non-fatal observations (masked host bits, unknown numeric
protocol, `any`+ports) are returned separately as `notes`.

Per-packet evaluation never returns a Go-level failure for a bad packet: it
answers HTTP **422** with `decision:"deny"`, `decided_by:"error"`,
`certain:false`, and the concrete reasons in a dedicated **`errors`** array.
Uncertain-but-processed results use a separate **`uncertainties`** array
(`certain:false`). The row is still persisted with a `request_id`, so failed
requests are traceable. If persistence itself fails, that fact is appended to
`uncertainties` and the answer is marked uncertain rather than appearing
untraceable.

## Architecture (real modules, not a hard-coded demo)

```
cmd/server          runnable HTTP entrypoint (flags)
server/             thin public package (external black-box entry)
internal/
  config/           JSON parsing + per-rule validation, error categories, notes
  netmodel/         interval-set algebra over big.Int + 5-D product spaces
  analyzer/         compilation + first-match shadow/redundancy analysis
  replay/           strict first-match packet evaluator with ordered steps
  store/            SQLite: config versions, reports, correlated request logs
  httpapi/          HTTP wiring, versioning, explainable responses
test/               INDEPENDENT Go module with its own first-match oracle
  oracle/           reference semantics + exhaustive enumeration (no reuse of
                    the implementation under test)
  harness/          in-process HTTP black box
  *_test.go         scenario + exhaustive comparison evidence
config/             demo fixture
scripts/            demo.sh, run_tests.sh
```

The geometric core (`internal/netmodel`) represents every axis as normalized
integer interval sets and a rule region as a union of axis-aligned products;
set difference is computed by recursively splitting products along axes.
This handles containment, partial overlaps and crossing intervals on
addresses *and* ports, and cannot be fooled by textual prefixes.

## HTTP API (local, JSON only)

| method/path | purpose |
|---|---|
| `POST /configs` | submit ruleset; returns version, parse result, notes, full report |
| `GET /reports/latest` | redirects to the newest stored report |
| `GET /reports/{version}` | report for a version |
| `POST /evaluate` | evaluate one packet; returns decision + ordered steps |
| `GET /requests/{id}` | correlated persisted log for a request id |
| `GET /healthz` | liveness + instance label |

Every evaluation response and log row carries `request_id`, config
`version`, `instance`, and `source_location`, plus the per-rule evaluation
`steps` (each with order, rule id, matched flag, and an explanation), so any
verdict can be re-derived and audited.

### Config format

```json
{
  "default_action": "deny",
  "rules": [
    {
      "id": "r1",
      "action": "allow",
      "protocol": "tcp",
      "source": "10.0.0.0/24",
      "destination": "10.0.2.0/24",
      "source_ports": "1024-65535",
      "destination_ports": "80-443"
    }
  ]
}
```

- `source` / `destination`: `""`, `*`, `any` for wildcard; a CIDR, or a
  comma-separated union of CIDRs (single family).
- `source_ports` / `destination_ports`: `*`/`any`/empty for all; a single
  number; or one inclusive `lo-hi` interval.

## Requirements

- Go 1.23+ (developed on 1.23.4). No CGO: the SQLite driver
  (`modernc.org/sqlite`) is pure Go.
- Python 3 is used only by the demo/test shell scripts for pretty printing.

## Reproduce it

```sh
# 1. full evidence: vet, build, unit tests, independent exhaustive suite
./scripts/run_tests.sh

# 2. live local walkthrough on an ephemeral port + temp database
./scripts/demo.sh

# 3. run the server yourself
go run ./cmd/server -addr 127.0.0.1:8080 -db netsem.db -instance local
curl -s -XPOST localhost:8080/configs \
  -H 'Content-Type: application/json' --data-binary @config/demo_rules.json
```

## What the evidence actually proves

- **Pointwise geometry** (`internal/netmodel/*_test.go`): interval and
  product set operations are checked by enumerating every point of small
  domains against a bitmask/membership oracle, including disjointness of the
  normalized difference.
- **Independent exhaustive comparison** (`test/`): a separate Go module with
  its own first-match reference (`oracle/`, sharing no code with the
  analyzer) enumerates the full cartesian product of deliberately small
  address/port/protocol spaces and asserts the running HTTP service decides
  identically for every packet.
- **Crossing intervals and rule swaps** (scenarios 1–2, 10): partial/full
  shadowing on crossing port intervals and on source-prefix containment, and
  that swapping two rules moves the finding exactly as first-match predicts
  (and flips the crossing packet's verdict).
- **Default deny / default allow** (scenarios 3–4): the default action
  participates; empty policy denies everything; default-allow makes an
  unreachable allow redundant while a real carve-out stays load-bearing.
- **Safe deletion** (deletion-invariance helper): for every rule flagged
  fully-shadowed or redundant, the test removes that rule, re-submits, and
  exhaustively re-evaluates, asserting the decision function is unchanged;
  the independent oracle separately counts per-rule flips so a rule that
  *would* change behavior is never called redundant.
- **Failure classes** (scenario 6): asserts the concrete `category` for each
  malformed input, not merely that an endpoint responded.
- **Unknowns and `any`** (scenarios 7–8): numeric unknown protocols match but
  are flagged uncertain; `any`+ports constrains only port-bearing protocols,
  verified exhaustively over tcp/udp/sctp/icmp/unknown.
- **Explainability/correlation** (scenario 9): request id joins the response
  to the persisted row; version, ordered steps, instance and source location
  round-trip; hard failures (`errors`) and uncertainties are listed
  separately; missing ids and missing configs return clean 404/409.

Tests are executed, not assumed: `run_tests.sh` runs the complete suite with
`-count=1` (no cache) and ends with `ALL TESTS PASSED`.
