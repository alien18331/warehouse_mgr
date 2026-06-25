"""Excel 匯入：目前支援 fig1（新竹採購進貨）。"""
from __future__ import annotations
from datetime import datetime, date
from typing import Optional
from openpyxl import load_workbook

from . import db


FIG1_HEADERS = [
    "到貨日期", "簽收人", "品牌", "產品名稱", "產品敘述",
    "序號", "進貨數量", "請購人員", "請購PO", "存放位置", "備註",
]


def _norm(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v).strip()


def _parse_qty(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_serials(v) -> list[str]:
    s = _norm(v)
    if not s:
        return []
    out = []
    for token in s.replace(",", "\n").replace("，", "\n").splitlines():
        token = token.strip()
        if token:
            out.append(token)
    return out


def _get_or_create(c, table: str, key_col: str, key_val: str,
                   extra: dict | None = None) -> int:
    row = c.execute(f"SELECT id FROM {table} WHERE {key_col}=?", (key_val,)).fetchone()
    if row:
        return row["id"]
    cols = [key_col] + list((extra or {}).keys())
    vals = [key_val] + list((extra or {}).values())
    placeholders = ",".join(["?"] * len(cols))
    cur = c.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({placeholders})", vals)
    return cur.lastrowid


def _get_or_create_product(c, brand_id: int, model: str, description: str) -> int:
    row = c.execute("SELECT id, description FROM products WHERE brand_id=? AND model=?",
                    (brand_id, model)).fetchone()
    if row:
        # 若原本沒有敘述但匯入有，補上
        if description and not row["description"]:
            c.execute("UPDATE products SET description=? WHERE id=?", (description, row["id"]))
        return row["id"]
    cur = c.execute("""INSERT INTO products(brand_id, model, description, base_unit, track_by_serial)
                       VALUES(?,?,?,?,0)""", (brand_id, model, description or None, "個"))
    return cur.lastrowid


def parse_fig1(file_bytes: bytes) -> list[dict]:
    """讀 fig1 sheet，回傳 dict 列表。空列已過濾。"""
    from io import BytesIO
    wb = load_workbook(BytesIO(file_bytes), data_only=True)
    if "fig1" not in wb.sheetnames:
        raise ValueError("Excel 內找不到 sheet「fig1」")
    ws = wb["fig1"]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [_norm(x) for x in rows[0]]
    # 容錯：允許欄位順序不同，按表頭名稱對應
    idx = {h: header.index(h) if h in header else None for h in FIG1_HEADERS}
    missing = [h for h, v in idx.items() if v is None and h in
               ("到貨日期", "品牌", "產品名稱", "進貨數量", "請購PO")]
    if missing:
        raise ValueError(f"fig1 缺少必要欄位：{', '.join(missing)}")

    out = []
    for i, raw in enumerate(rows[1:], start=2):  # row 2 = first data row (1-indexed)
        def cell(name):
            j = idx[name]
            return raw[j] if j is not None and j < len(raw) else None

        if all(c is None or _norm(c) == "" for c in raw):
            continue

        out.append({
            "row_no": i,
            "date": _norm(cell("到貨日期")),
            "signer": _norm(cell("簽收人")),
            "brand": _norm(cell("品牌")),
            "model": _norm(cell("產品名稱")),
            "description": _norm(cell("產品敘述")),
            "serials": _parse_serials(cell("序號")),
            "qty": _parse_qty(cell("進貨數量")),
            "requester": _norm(cell("請購人員")),
            "po_no": _norm(cell("請購PO")),
            "location": _norm(cell("存放位置")),
            "note": _norm(cell("備註")),
        })
    return out


def import_fig1(file_bytes: bytes, dry_run: bool = False) -> dict:
    """匯入 fig1（新竹採購進貨）。
    回傳統計：{groups, rows, lines_inserted, skipped_existing_po, errors, dry_run}"""
    rows = parse_fig1(file_bytes)
    errors = []
    # 驗證
    valid_rows = []
    for r in rows:
        msgs = []
        if not r["date"]:
            msgs.append("缺到貨日期")
        if not r["brand"]:
            msgs.append("缺品牌")
        if not r["model"]:
            msgs.append("缺產品名稱")
        if r["qty"] <= 0:
            msgs.append("數量需 > 0")
        if not r["po_no"]:
            msgs.append("缺請購PO")
        if msgs:
            errors.append({"row": r["row_no"], "msgs": msgs})
            continue
        valid_rows.append(r)

    # 依 (date, po_no, signer) 分組
    groups: dict[tuple, list[dict]] = {}
    for r in valid_rows:
        key = (r["date"], r["po_no"], r["signer"])
        groups.setdefault(key, []).append(r)

    stats = {
        "total_rows": len(rows),
        "valid_rows": len(valid_rows),
        "groups": len(groups),
        "lines_inserted": 0,
        "groups_inserted": 0,
        "skipped_existing_po": 0,
        "errors": errors,
        "dry_run": dry_run,
        "details": [],
    }

    if dry_run:
        # 預覽：列出每組要做什麼，但不寫入
        for (d, po, sg), lines in groups.items():
            stats["details"].append({
                "date": d, "po_no": po, "signer": sg, "lines": len(lines),
                "preview": [f'{ln["brand"]} {ln["model"]} x{ln["qty"]} → {ln["location"]}' for ln in lines],
            })
        return stats

    with db.tx() as c:
        for (d, po_no, signer), lines in groups.items():
            # 若 PO 已存在於 inbound_orders 則整組跳過
            existing = c.execute("""SELECT io.id FROM inbound_orders io
                                    LEFT JOIN purchase_orders po ON po.id=io.po_id
                                    WHERE po.po_no=?""", (po_no,)).fetchone()
            if existing:
                stats["skipped_existing_po"] += 1
                stats["details"].append({
                    "date": d, "po_no": po_no, "signer": signer,
                    "lines": len(lines), "action": "skipped (PO 已存在)",
                })
                continue

            signer_id = _get_or_create(c, "staff", "name", signer, {"role": "簽收"}) if signer else None
            # 用同一個請購人員（取群組第一行）
            requester_name = next((ln["requester"] for ln in lines if ln["requester"]), "")
            requester_id = _get_or_create(c, "staff", "name", requester_name, {"role": "請購"}) if requester_name else None

            # PO 主檔
            po_row = c.execute("SELECT id FROM purchase_orders WHERE po_no=?", (po_no,)).fetchone()
            if po_row:
                po_id = po_row["id"]
                if requester_id:
                    c.execute("UPDATE purchase_orders SET requester_id=COALESCE(requester_id, ?) WHERE id=?",
                              (requester_id, po_id))
            else:
                cur = c.execute("INSERT INTO purchase_orders(po_no, date, requester_id) VALUES(?,?,?)",
                                (po_no, d, requester_id))
                po_id = cur.lastrowid

            # 群組備註（取所有非空 note 串接）
            notes = [ln["note"] for ln in lines if ln["note"]]
            note_text = "; ".join(dict.fromkeys(notes))  # dedupe 保序

            cur = c.execute("""INSERT INTO inbound_orders(type, date, signer_id, po_id, note)
                               VALUES('hsinchu', ?, ?, ?, ?)""",
                            (d, signer_id, po_id, note_text or None))
            in_id = cur.lastrowid
            stats["groups_inserted"] += 1

            for ln in lines:
                brand_id = _get_or_create(c, "brands", "name", ln["brand"])
                prod_id = _get_or_create_product(c, brand_id, ln["model"], ln["description"])
                loc_id = _get_or_create(c, "locations", "code", ln["location"]) if ln["location"] else None
                cur2 = c.execute("""INSERT INTO inbound_lines(inbound_id, product_id, qty, unit,
                                    location_id, is_surplus) VALUES(?,?,?,?,?,0)""",
                                 (in_id, prod_id, ln["qty"], "個", loc_id))
                line_id = cur2.lastrowid
                stats["lines_inserted"] += 1

                for sn in ln["serials"]:
                    c.execute("""INSERT OR IGNORE INTO serial_items(product_id, serial_no, status,
                                 current_location_id, inbound_line_id, is_surplus)
                                 VALUES(?,?,?,?,?,0)""",
                              (prod_id, sn, "in_stock", loc_id, line_id))

            stats["details"].append({
                "date": d, "po_no": po_no, "signer": signer,
                "lines": len(lines), "action": "imported",
            })

    return stats
