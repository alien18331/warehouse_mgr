# -*- coding: utf-8 -*-
"""工號合併：把「同一工號的不同寫法」併成一個。

背景：建立工號的四個入口過去只做完整字串比對，於是 `J114-10-295(JU)` 與
`J114-10-295` 並存成兩個工號。兩邊各自持有庫存，系統把同工號內的調撥記成
跨工號借用，導致進出貨歷史的展開明細出現幻影庫存。

本腳本把來源工號的所有參照改指到目標工號，**不刪除任何明細資料**。
來源的 projects 列預設改名標記（可逆），加 --delete-source 才真的刪除。

用法：
    python tools/merge_projects.py                      # 試跑，不寫入
    python tools/merge_projects.py --apply              # 執行（來源改名標記）
    python tools/merge_projects.py --apply --delete-source
    python tools/merge_projects.py --apply --db path/to/copy.db
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import canon_job_no  # noqa: E402

# (來源 id, 目標 id)：來源被吸收，目標保留
MERGES = [
    (44, 57),    # J114-10-295(JU)  → J114-10-295
    (80, 33),    # 115-04-144       → J115-04-144
    (462, 49),   # J114-11-330(JP)  → J114-11-330
    (59, 56),    # J114-11-336(JC)  → J114-11-336
    (60, 55),    # J114-11-337(JC)  → J114-11-337
]

# 所有參照 projects(id) 的外鍵欄位。漏一個就會留下孤兒。
FK_COLS = [
    ("inbound_orders", "project_id"),
    ("outbound_orders", "project_id"),
    ("outbound_orders", "borrow_to_project_id"),
    ("outbound_lines", "from_project_id"),
    ("inbound_lines", "project_id"),
    ("serial_items", "project_id"),
    ("project_transfers", "from_project_id"),
    ("project_transfers", "to_project_id"),
]

MERGED_TAG = "｜已併入 "


def existing_fk_cols(c):
    """只保留這個 DB 真的存在的表／欄位，讓腳本能跨 schema 版本重跑。"""
    out = []
    for t, col in FK_COLS:
        try:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({t})").fetchall()}
        except sqlite3.Error:
            continue
        if cols and col in cols:
            out.append((t, col))
    return out


def prod_totals(c):
    return {r["product_id"]: r["q"] for r in
            c.execute("SELECT product_id, SUM(qty) q FROM stock_balance GROUP BY product_id")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="預設為專案根目錄的 warehouse.db")
    ap.add_argument("--apply", action="store_true", help="實際寫入；省略則為試跑")
    ap.add_argument("--delete-source", action="store_true",
                    help="刪除來源 projects 列（不可逆）；預設改名標記")
    ap.add_argument("--keep-notes", action="store_true",
                    help="保留合併後變成「借自自己」的註記")
    ap.add_argument("--keep-raw-imports", action="store_true",
                    help="不更新 raw_imports.project_no 的舊工號字串")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else Path(__file__).resolve().parent.parent / "warehouse.db"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    c = con.cursor()

    names = {r["id"]: r["job_no"] for r in c.execute("SELECT id, job_no FROM projects")}
    cols = existing_fk_cols(c)

    # ---- 前置檢查 ----
    # plan        = 尚未合併，需要改指外鍵的來源
    # delete_only = 先前已合併並改名標記過的來源；參照應已為 0，
    #               只在 --delete-source 時需要處理（否則無事可做）
    plan, delete_only = [], []
    for src, dst in MERGES:
        if src not in names:
            print(f"  跳過 id{src}：工號不存在（可能已刪除）")
            continue
        if dst not in names:
            sys.exit(f"目標工號 id{dst} 不存在，中止。")
        if MERGED_TAG in names[src]:
            # 已標記過：外鍵早就改指完了，只剩「刪或不刪」
            delete_only.append((src, dst))
            continue
        if canon_job_no(names[src]) != canon_job_no(names[dst]):
            sys.exit(f"id{src} 「{names[src]}」與 id{dst} 「{names[dst]}」"
                     f"正規化後不相等，拒絕合併。")
        plan.append((src, dst))

    if not plan and not (delete_only and args.delete_source):
        if delete_only:
            print(f"這 {len(delete_only)} 個來源工號已合併並標記過；"
                  f"要刪除它們請加 --delete-source。")
            for src, dst in delete_only:
                print(f"  id{src} {names[src]}")
        else:
            print("沒有需要合併的工號。")
        return

    print(f"DB: {db_path}")
    print(f"模式: {'執行' if args.apply else '試跑（不寫入）'}"
          f"{'  來源列將被刪除' if args.delete_source else '  來源列改名標記'}\n")
    print("=== 合併計畫 ===")
    for src, dst in plan:
        print(f"  {names[src]:<24}(id{src:<4}) → {names[dst]:<22}(id{dst})")
    for src, dst in (delete_only if args.delete_source else []):
        print(f"  {names[src]:<40}(id{src:<4}) 已合併過 → 僅刪除來源列")

    before = prod_totals(c)
    # 待處理的來源 = 本次要改指的 + 先前已標記而這次要刪的
    todo = plan + (delete_only if args.delete_source else [])
    srcs = [x for x, _ in todo]
    qmark = ",".join("?" * len(srcs))

    c.execute("SAVEPOINT merge")

    # ---- 1. 外鍵改指 ----
    print("\n=== 外鍵改指 ===")
    total = 0
    for t, col in cols:
        n = 0
        for src, dst in plan:
            c.execute(f"UPDATE {t} SET {col}=? WHERE {col}=?", (dst, src))
            n += c.rowcount
        total += n
        if n:
            print(f"  {t}.{col:<22} {n:>4} 列")
    print(f"  合計 {total} 列")

    # ---- 2. 斷言來源參照歸零 ----
    leftover = []
    for t, col in cols:
        n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} IN ({qmark})", srcs).fetchone()[0]
        if n:
            leftover.append(f"{t}.{col}={n}")
    if leftover:
        c.execute("ROLLBACK TO merge")
        sys.exit("來源工號仍有殘留參照，已回復：" + ", ".join(leftover))
    print("  來源工號參照已全部歸零")

    # ---- 3. 清掉「借自自己」的註記 ----
    if not args.keep_notes:
        rows = c.execute("""SELECT ol.id, ol.note FROM outbound_lines ol
                            JOIN outbound_orders oo ON oo.id = ol.outbound_id
                            WHERE ol.note IS NOT NULL AND ol.note LIKE '%借自%'
                              AND ol.from_project_id IS NOT NULL
                              AND ol.from_project_id = oo.project_id""").fetchall()
        n = 0
        for r in rows:
            # 只移除「借自 XXX」這一段，其餘註記（例如「源自 … 餘料」、人工備註）保留
            new = re.sub(r"借自\s*[^；;，,]*", "", r["note"])
            new = re.sub(r"[；;，,]\s*[；;，,]", "；", new).strip("；;，, ")
            new = re.sub(r"\s{2,}", " ", new).strip() or None
            if new != r["note"]:
                c.execute("UPDATE outbound_lines SET note=? WHERE id=?", (new, r["id"]))
                n += 1
        print(f"\n=== 清除「借自自己」註記：{n} 列 ===")

    # ---- 4. raw_imports 文字同步 ----
    if not args.keep_raw_imports:
        try:
            n = 0
            for src, dst in plan:
                c.execute("UPDATE raw_imports SET project_no=? WHERE project_no=?",
                          (names[dst], names[src]))
                n += c.rowcount
            print(f"=== raw_imports.project_no 同步為標準寫法：{n} 列 ===")
        except sqlite3.Error as e:
            print(f"=== raw_imports 略過（{e}）===")

    # ---- 5. 來源 projects 列 ----
    if args.delete_source:
        n = 0
        for src, _ in todo:
            c.execute("DELETE FROM projects WHERE id=?", (src,))
            n += c.rowcount
        if n != len(todo):
            c.execute("ROLLBACK TO merge")
            sys.exit(f"預期刪除 {len(todo)} 列，實際 {n} 列，已回復。")
        print(f"=== 已刪除來源 projects 列：{n} 列 ===")
    else:
        for src, dst in plan:
            c.execute("UPDATE projects SET job_no=? WHERE id=?",
                      (f"{names[src]}{MERGED_TAG}{names[dst]}", src))
        print(f"=== 來源 projects 列已改名標記：{len(plan)} 列 ===")

    # ---- 6. 對帳 ----
    after = prod_totals(c)
    drift = [(p, before.get(p, 0), after.get(p, 0)) for p in set(before) | set(after)
             if abs(before.get(p, 0) - after.get(p, 0)) > 1e-9]
    neg = c.execute("SELECT COUNT(*) FROM stock_balance WHERE qty < -1e-9").fetchone()[0]
    fk = len(c.execute("PRAGMA foreign_key_check").fetchall())
    print("\n=== 對帳 ===")
    print(f"  各料件總庫存有變化的料件數: {len(drift)}   (必須為 0)")
    print(f"  負數庫存桶: {neg}")
    print(f"  外鍵違反: {fk}")
    if drift or neg or fk:
        c.execute("ROLLBACK TO merge")
        sys.exit("對帳未通過，已回復，未寫入任何變更。")

    if args.apply:
        c.execute("RELEASE merge")
        con.commit()
        print("\n已提交。")
    else:
        c.execute("ROLLBACK TO merge")
        con.rollback()
        print("\n試跑結束，已回復，未寫入任何變更。加 --apply 才會實際執行。")
    con.close()


if __name__ == "__main__":
    main()
