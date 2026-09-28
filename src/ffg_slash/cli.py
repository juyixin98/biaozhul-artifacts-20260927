"""Command line entry points.

    python -m ffg_slash serve  --config configs/demo.json
    python -m ffg_slash replay --config configs/demo.json --feed data/demo_feed.jsonl
    python -m ffg_slash gen-demo --out configs/demo.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .replay import replay


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ffg_slash")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the FastAPI HTTP service")
    serve.add_argument("--config", default="configs/demo.json")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8088)

    rep = sub.add_parser("replay", help="offline replay a JSONL/JSON feed")
    rep.add_argument("--config", default="configs/demo.json")
    rep.add_argument("--feed", required=True)
    rep.add_argument("--db")
    rep.add_argument("--json", action="store_true", help="emit report as JSON")

    gen = sub.add_parser("gen-demo", help="write the synthetic demo config")
    gen.add_argument("--out", default="configs/demo.json")

    args = parser.parse_args(argv)

    if args.cmd == "gen-demo":
        _write_demo_config(Path(args.out))
        print(f"wrote {args.out}")
        return 0

    config = load_config(args.config)

    if args.cmd == "replay":
        report = replay(config, args.feed, db_path=args.db)
        if args.json:
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            print(f"run {report.run_id}: total={report.total} accepted={report.accepted} "
                  f"duplicate={report.duplicate} rejected={report.rejected_by_reason}")
            print(f"invalid_signatures={report.invalid_signatures} "
                  f"real_conflicts={report.real_conflicts_total} "
                  f"evidence={len(report.evidences)}")
        return 0

    if args.cmd == "serve":
        import uvicorn
        from .app import create_app
        app = create_app(config)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0

    parser.error("unknown command")
    return 2


def _write_demo_config(path: Path) -> None:
    from .crypto import derive_seed, keypair_from_seed
    path.parent.mkdir(parents=True, exist_ok=True)
    labels = ["alpha", "bravo", "charlie", "delta"]
    # epochs 1/2 use 4 validators weight 1; epoch 3 rotates membership
    def members(labels_, weights=None):
        out = []
        for i, label in enumerate(labels_):
            seed = derive_seed(label)
            _, pub = keypair_from_seed(seed)
            w = (weights or {}).get(label, 1)
            out.append({"derive": label, "weight": w, "pubkey": pub.hex()})
        return out

    config = {
        "chain_id": 4242,
        "genesis_root": "11" * 32,
        "database": "data/demo.sqlite3",
        "log_dir": "logs",
        "epochs": [
            {"epoch": 1, "validators": members(labels)},
            {"epoch": 2, "validators": members(labels)},
            # membership change: delta leaves, echo joins
            {"epoch": 3, "validators": members(["alpha", "bravo", "charlie", "echo"])},
            {"epoch": 4, "validators": members(["alpha", "bravo", "charlie", "echo"])},
        ],
    }
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
