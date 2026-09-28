# Specification — `smt-v1` sparse Merkle commitment

This document fixes the byte-level rules. The production kernel and the
independent test reference both implement exactly these rules.

## 1. Parameters

- Key width: **256 bits / 32 bytes**, fixed. Keys never vary in length.
- Hash: **SHA-256**, 32-byte output (OpenSSL via `hashlib`).
- Every value is an opaque byte string. A present value of length 0 (`b""`)
  is a **real leaf** and is distinct in commitment from a missing key.

## 2. Domain-separated preimages

All preimages start with a one-byte tag, so the three node kinds can never
collide:

| tag | kind | preimage |
| --- | --- | --- |
| `0x00` | leaf | `TAG_LEAF ‖ key(32) ‖ u16be(len(value)) ‖ value` |
| `0x01` | branch | `TAG_BRANCH ‖ u16be(32) ‖ left(32) ‖ u16be(32) ‖ right(32)` |
| `0x02smt-v1-empty` | empty | `TAG_EMPTY ‖ u16be(depth)` (base) / with children (below) |

`u16be` is a 2-byte big-endian length prefix. The leaf binds **both the full
key and the value**; swapping either changes the commitment.

### Empty subtree hashes, defined level by level

```
empty[256] = SHA256( TAG_EMPTY ‖ u16be(256) )                    # empty leaf slot
empty[d]   = SHA256( TAG_EMPTY ‖ u16be(d) ‖ empty[d+1] ‖ empty[d+1] )   # 255 ≥ d ≥ 0
```

Each level commits to its depth and to the two empty subtrees below it; the
257 values are all distinct. The trie root over an empty database is
`empty[0]`. Because empty nodes use tag `0x02`, "this subtree is empty" can
never be presented as a leaf or branch.

## 3. Canonical tree shape / determinism

Nodes are **content-addressed**: node id = SHA-256(preimage). Empty subtrees
are virtual (derived, never stored).

Branches are kept canonical so the root is a pure function of the
`(key → value)` map, independent of update order:

- a branch with two empty children collapses to `empty[d]` (nothing stored);
- a branch with one empty child collapses by **hoisting the other child only
  when it is a leaf**. A leaf carries the full 256-bit key and can be
  membership-checked at any depth, so hoisting is safe. A *branch* child is
  never hoisted, because a branch blob does not encode its depth — reading it
  at a shallower depth would interpret the key bits at wrong indices.

Consequences (all asserted by tests):

- inserting the same set in any order yields one root;
- `batch` = sort items by ascending key then apply; it equals the sequential
  root;
- deleting a key yields the canonical root of the remaining set; deleting
  then re-inserting the same value restores the **exact** previous root;
- deleting an absent key is a no-op and writes no journal record.

When two different keys meet at a leaf, the trie splits down to the first
differing bit; distinct 256-bit keys differ before depth 256.

## 4. Proofs

Proof object (JSON), for queried key `key` at claimed root `root`:

```jsonc
{
  "version": "smt-v1",
  "root": "<32-byte hex>",
  "key":  "<32-byte hex>",
  "exists": true | false,
  "terminal_depth": <0..256>,
  "terminal": { "kind": "empty" }
            | { "kind": "leaf", "key": "<32-byte hex>", "value": "<hex>" },
  "steps": [ /* compressed off-path path, ordered from depth 0 upward */ ]
}
```

Step entries:

- `{"kind":"sibling","depth":d,"sibling_hash":"<32-byte hex>"}` — the off-path
  subtree rooted at depth `d+1`;
- `{"kind":"empty_run","depth":d,"length":n}` — the next `n` levels
  (`d … d+n-1`) all have the virtual `empty[level+1]` as sibling. **This is
  the compressed path; a verifier must expand it into one sibling per level**
  before folding.

Terminal kinds:

- `empty`: the walk reached a virtual empty subtree — proves **absence**;
- `leaf` with `exists=true`: membership; the terminal key **must equal** the
  queried key;
- `leaf` with `exists=false`: a diverging neighbor leaf witnesses **absence**;
  the terminal key must differ from the queried key and share its first
  `terminal_depth` bits.

### Verification

1. Validate widths/types and the version.
2. Compute the terminal commitment: `empty[terminal_depth]`, or
   `leafHash(terminal.key, terminal.value)`; enforce the key-binding rules
   above (membership key equality; non-membership prefix agreement).
3. Expand `steps` into one sibling per level `0..terminal_depth-1`, checking
   that entries are contiguous (`depth == covered`) and runs have positive
   length that does not overshoot the terminal.
4. Fold from the terminal **upward** to depth 0:
   `h = branchHash(h, sibling)` if key bit at that depth is 0, else
   `branchHash(sibling, h)`.
5. Accept iff `h == root`.

The proof therefore binds **root, key and depth** together; changing any of
them, a sibling hash, a run length/depth, or the terminal yields one of the
stable failure categories below.

### Failure categories

`malformed`, `key_mismatch`, `prefix_mismatch`, `terminal_invalid`,
`step_invalid`, `root_mismatch`. The independent reference checker uses the
same category strings.

## 5. Journal records and offline replay

Each effective change appends a signed record. The signed canonical payload
(sorted-key compact JSON) is:

```json
{"version":"smt-v1","kind":"set|delete","key_hex":"…","value_hex":"…|null",
 "prev_root":"<hex>","new_root":"<hex>"}
```

`signature_hex = HMAC-SHA256(derivedKey, "smt-v1-journal" ‖ canonicalJSON)`,
verified with a constant-time comparison. Records carry a contiguous `seq`
and chain `new_root[i] == prev_root[i+1]`.

The offline replayer trusts neither the author's roots nor the ordering: it
verifies each HMAC, checks `prev_root` against its own running root, replays
the change on a fresh tree, and requires its recomputed root to equal the
signed `new_root`. The first failure aborts with a category and the record
`seq` (`bad_signature`, `sequence_gap`, `chain_break`, `root_mismatch`,
`bad_record`).

## 6. History

Stored nodes are immutable and never pruned, so a proof issued against an old
`root` remains verifiable after the current root advances. Revisions
(genesis root included) are listed via `GET /api/v1/revisions`.
