#!/usr/bin/env python3
"""Run the demo MERGE request locally, print the action set, and exit.

Uses the in-process service (no server needed):
    .venv/bin/python scripts/seed_demo.py data/merge-demo.db
    .venv/bin/python scripts/run_demo.py examples/demo_merge.json \
        --db data/merge-demo.db --validate     # plan only
    .venv/bin/python scripts/run_demo.py examples/demo_merge.json \
        --db data/merge-demo.db                # commit
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merge_engine.service import MergeService  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request_json")
    ap.add_argument("--db", default="data/merge-demo.db")
    ap.add_argument("--log", default="logs/merge-runs.jsonl")
    ap.add_argument("--validate", action="store_true",
                    help="only build the plan, do not commit")
    ap.add_argument("--fail-commit", action="store_true",
                    help="inject a commit failure (rollback demonstration)")
    args = ap.parse_args()

    with open(args.request_json, encoding="utf-8") as fh:
        request = json.load(fh)

    svc = MergeService(args.db, log_path=args.log)
    try:
        if args.validate:
            result = svc.validate(request)
        else:
            result = svc.merge(request, failpoint="commit" if args.fail_commit else None)
    except Exception as exc:
        print(json.dumps(getattr(exc, "to_dict", lambda: {"error": str(exc)})(),
                         indent=2, ensure_ascii=False))
        return 1
    finally:
        svc.close()

    print(f"run_id: {result['run_id']} (seq {result['run_seq']})")
    print(f"status: {result['status']}")
    print("summary:", json.dumps(result["summary"], ensure_ascii=False))
    print("actions:")
    for a in result["actions"]:
        vals = json.dumps(a["new_values"], ensure_ascii=False)
        print(f"  #{a['seq']:>2} {a['outcome']:<10} key={a['key']} "
              f"src_idx={a['source_index']} tgt_rowid={a['target_rowid']} {vals}")
    print("decisions (why):")
    for d in result["decisions"]:
        print(f"  src_idx={d['source_index']} matched={d['matched']} "
              f"-> {d['outcome']} via clause {d['fired_clause']}: {d['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
