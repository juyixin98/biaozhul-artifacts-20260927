# Reusable deterministic fixtures

These files are generated deterministically by

```bash
cargo run -p hbs-cli -- gen-fixtures --out tests/fixtures/generated
```

(also run by `scripts/verify.sh`). They are safe to delete and regenerate.

Each scenario `<label>` (distribution × seed) produces three files:

| file | role |
|---|---|
| `<label>.values.json` | **Ground truth**: the sorted unique `u32` inputs. Never produced by decoding an `.hbs` file, so a broken encoder cannot poison the expected answers. |
| `<label>.hbs` | The set encoded by the SUT (format v1). Used for round-trip and byte-corruption rejection tests. |
| `<label>.meta.json` | Expected cardinality and chunk/container statistics. |

Distributions:

- `sparse` — ~200 values across the `u32` universe; every chunk an array.
- `dense` — three 30k-value runs; three bitmap chunks.
- `interleaved` — 24 chunks alternating sparse (~20) and dense (~6000).
- `container_threshold` — chunk sizes exactly 4095 / 4096 (array) and
  4097 (bitmap), pinning the fixed switching threshold from both sides.

Seeds `1`, `42`, `777` make the set fully reproducible; a scenario is
regenerated from `(distribution, seed)` alone.
