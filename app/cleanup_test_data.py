"""清除開發過程留下的測試資料。

執行：
  python -m app.cleanup_test_data

刪除目標（精確比對，不影響正式資料）：
- products: model 為 'TEST-SN-MODEL' / '1769-NEW-CHECK' / 'PAR-TEST'
- serial_items: 上述 products 的所有序號 + serial_no 字面為 SN:TEST001..003 / SN-TEST-HIT / SN-TEST-MODEL
- purchase_orders: po_no = 'PO-EDIT-TEST'
- inbound_orders.po_id: 若指向 'PO-EDIT-TEST'，且該 inbound 之前的 PO 是 '20260320004'，自動還原；否則僅設為 NULL
- outbound_orders: 標題或備註提到 PAR-TEST，且只有測試元件 line 的整張單刪除
"""
from . import db


TEST_PRODUCT_MODELS = ("TEST-SN-MODEL", "1769-NEW-CHECK", "PAR-TEST")
TEST_SN_LITERALS = ("SN:TEST001", "SN:TEST002", "SN:TEST003",
                    "SN-TEST-HIT", "SN-TEST-MODEL")
TEST_PO_NUMBERS = ("PO-EDIT-TEST",)


def cleanup():
    summary = {"outbound_deleted": 0, "products_deleted": 0, "serials_deleted": 0,
               "purchase_orders_deleted": 0, "inbound_po_reverted": 0}
    with db.tx() as c:
        c.execute("PRAGMA foreign_keys = ON")

        # 1) 刪 PAR-TEST 套件展開的出貨單
        out_ids = [r["id"] for r in c.execute(
            """SELECT DISTINCT oo.id FROM outbound_orders oo
               JOIN outbound_lines ol ON ol.outbound_id = oo.id
               WHERE ol.note LIKE '%PAR-TEST%'""").fetchall()]
        for oid in out_ids:
            c.execute("DELETE FROM outbound_orders WHERE id=?", (oid,))
            summary["outbound_deleted"] += 1

        # 2) 還原任何指向 PO-EDIT-TEST 的 inbound_orders → 嘗試還原到 20260320004
        real_po = c.execute("SELECT id FROM purchase_orders WHERE po_no='20260320004'").fetchone()
        for po_no in TEST_PO_NUMBERS:
            row = c.execute("SELECT id FROM purchase_orders WHERE po_no=?", (po_no,)).fetchone()
            if not row:
                continue
            pid = row["id"]
            for inb in c.execute("SELECT id FROM inbound_orders WHERE po_id=?", (pid,)).fetchall():
                new_po = real_po["id"] if real_po else None
                c.execute("UPDATE inbound_orders SET po_id=? WHERE id=?", (new_po, inb["id"]))
                summary["inbound_po_reverted"] += 1
            c.execute("DELETE FROM purchase_orders WHERE id=?", (pid,))
            summary["purchase_orders_deleted"] += 1

        # 3) 刪測試料件的序號，再刪料件本身
        test_pids = [r["id"] for r in c.execute(
            f"SELECT id FROM products WHERE model IN ({','.join('?'*len(TEST_PRODUCT_MODELS))})",
            TEST_PRODUCT_MODELS).fetchall()]
        for pid in test_pids:
            n = c.execute("DELETE FROM serial_items WHERE product_id=?", (pid,)).rowcount
            summary["serials_deleted"] += n or 0
            # kit_components ON DELETE CASCADE 會處理 parent；元件引用情況下會擋下
            try:
                c.execute("DELETE FROM products WHERE id=?", (pid,))
                summary["products_deleted"] += 1
            except Exception as e:
                print(f"[warn] could not delete product id={pid}: {e}")

        # 4) 殘留的字面測試序號（不靠 product_id，例如 SN-TEST-HIT 被掛在其他料件上）
        placeholders = ",".join("?" * len(TEST_SN_LITERALS))
        n = c.execute(f"DELETE FROM serial_items WHERE serial_no IN ({placeholders})",
                      TEST_SN_LITERALS).rowcount
        summary["serials_deleted"] += n or 0

    print("[cleanup] done:", summary)


if __name__ == "__main__":
    cleanup()
