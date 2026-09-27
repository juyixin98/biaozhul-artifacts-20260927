#!/usr/bin/env python3
"""用 fixtures/documents.json 重建本地 SQLite 库（文档 + 倒排索引）。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from searchdsl.config import load_settings
from searchdsl.index import Index
from searchdsl.store import Store


def main() -> None:
    settings = load_settings()
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "documents.json"
    documents = json.loads(fixtures.read_text(encoding="utf-8"))
    store = Store(settings.database_path)
    index = Index(store, settings.schema)
    postings = index.rebuild(documents)
    print(f"重建完成：{len(documents)} 篇文档，{postings} 条倒排记录 -> {settings.database_path}")
    store.close()


if __name__ == "__main__":
    main()
