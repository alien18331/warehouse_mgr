"""手動執行範例資料 seed。

執行：
  python -m app.seed

所有 INSERT 皆使用 INSERT OR IGNORE，已存在的資料不會被覆蓋。
"""
from . import db

if __name__ == "__main__":
    db.init_db()
    print(f"[seed] DB file: {db.DB_PATH}")
    db.seed_sample_data()
    print("[seed] done. (existing rows untouched)")
