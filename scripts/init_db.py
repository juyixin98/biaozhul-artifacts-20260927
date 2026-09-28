"""用种子 JSONL 初始化 SQLite 并创建激活版本。

用法:
    python -m scripts.init_db                 # 使用 config/settings.json
    SPELLCHECK_DB_PATH=/tmp/x.db python -m scripts.init_db
"""
from __future__ import annotations

from app.config import load_settings
from app.lexicon import connect, create_version, get_active_version, load_jsonl


def main() -> None:
    settings = load_settings()
    conn = connect(settings.db_file)
    existing = get_active_version(conn)
    if existing:
        print(f"已存在激活版本 {existing}，跳过初始化（如需重建请删除数据库文件）")
        return
    entries = load_jsonl(settings.seed_file)
    vid = create_version(conn, entries, version_id="seed-v1", source=str(settings.seed_file))
    print(f"已创建并激活版本 {vid}，共 {len(entries)} 条 -> {settings.db_file}")


if __name__ == "__main__":
    main()
