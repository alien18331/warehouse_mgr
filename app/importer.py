"""Excel 匯入：目前支援 fig1（新竹採購進貨）。"""
from __future__ import annotations
from datetime import datetime, date
from typing import Optional
from openpyxl import load_workbook

from . import db


FIG1_HEADERS = [
    "到貨日期", "簽收人", "供應商", "品牌", "產品名稱",
    "序號", "進貨數量", "請購人員", "請購PO", "存放位置",
]
# 供應商欄位的可接受別名（規範名為「供應商」）
SUPPLIER_ALIASES = ("供應商", "出貨對象")


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


PARTS_MODEL_ALIASES = ("料件", "型號", "產品名稱", "新增料件")
PARTS_DESC_ALIASES = ("敘述", "產品敘述", "描述", "簽收人")  # 簽收人為舊檔誤植，沿用相容


def build_parts_template() -> bytes:
    """產生一份只有表頭 + 一筆示範資料的料件主檔範本。"""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook(); ws = wb.active; ws.title = "料件"
    ws.append(["品牌", "料件", "敘述"])
    hf = Font(bold=True, color="FFFFFF"); hb = PatternFill("solid", fgColor="1F3A5F")
    for cell in ws[1]:
        cell.font = hf; cell.fill = hb; cell.alignment = Alignment(horizontal="center")
    ws.append(["AB", "1756-IB32", "10-31 VDC INPUT 32 PTS (36 PIN)"])
    for col, w in zip("ABC", (12, 22, 50)):
        ws.column_dimensions[col].width = w
    buf = BytesIO(); wb.save(buf); return buf.getvalue()


def import_parts(file_bytes: bytes, dry_run: bool = False) -> dict:
    """匯入料件主檔。強制 is_kit=0。已存在的 (brand, model) 直接跳過。"""
    from io import BytesIO
    wb = load_workbook(BytesIO(file_bytes), data_only=True)
    if not wb.sheetnames:
        raise ValueError("Excel 內沒有任何 sheet")
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {"total_rows": 0, "valid_rows": 0, "inserted": 0, "skipped_existing": 0,
                "errors": [], "dry_run": dry_run, "details": []}
    header = [_norm(x) for x in rows[0]]

    def find_idx(canonical, aliases):
        if canonical in header:
            return header.index(canonical)
        for a in aliases:
            if a in header:
                return header.index(a)
        return None

    brand_idx = header.index("品牌") if "品牌" in header else None
    model_idx = find_idx("料件", PARTS_MODEL_ALIASES)
    desc_idx = find_idx("敘述", PARTS_DESC_ALIASES)

    missing = []
    if brand_idx is None: missing.append("品牌")
    if model_idx is None: missing.append("料件 (可接受：料件/型號/產品名稱/新增料件)")
    if missing:
        raise ValueError(f"缺少必要欄位：{', '.join(missing)}")

    errors = []
    cleaned = []
    for i, raw in enumerate(rows[1:], start=2):
        if all(c is None or _norm(c) == "" for c in raw):
            continue
        brand = _norm(raw[brand_idx]) if brand_idx < len(raw) else ""
        model = _norm(raw[model_idx]) if model_idx < len(raw) else ""
        desc = _norm(raw[desc_idx]) if (desc_idx is not None and desc_idx < len(raw)) else ""
        msgs = []
        if not brand: msgs.append("缺品牌")
        if not model: msgs.append("缺料件")
        if msgs:
            errors.append({"row": i, "msgs": msgs}); continue
        cleaned.append({"row": i, "brand": brand, "model": model, "description": desc})

    stats = {
        "total_rows": len(rows) - 1,
        "valid_rows": len(cleaned),
        "inserted": 0,
        "skipped_existing": 0,
        "errors": errors,
        "dry_run": dry_run,
        "details": [],
    }

    if dry_run:
        for r in cleaned:
            stats["details"].append({**r, "action": "would-insert"})
        return stats

    with db.tx() as c:
        for r in cleaned:
            brand_id = _get_or_create(c, "brands", "name", r["brand"])
            existing = c.execute("SELECT id FROM products WHERE brand_id=? AND model=?",
                                 (brand_id, r["model"])).fetchone()
            if existing:
                stats["skipped_existing"] += 1
                stats["details"].append({**r, "action": "skipped (已存在)"})
                continue
            c.execute("""INSERT INTO products(brand_id, model, description, base_unit,
                         track_by_serial, safety_stock, is_kit)
                         VALUES(?,?,?,?,?,?,0)""",
                      (brand_id, r["model"], r["description"] or None, "個", 0, 0))
            stats["inserted"] += 1
            stats["details"].append({**r, "action": "inserted"})
    return stats


def build_fig1_template() -> bytes:
    """產生一份只有表頭 + 一筆示範資料的 xlsx 範本。"""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "進貨"
    ws.append(FIG1_HEADERS)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    # 一筆示範資料 — 上線後請刪掉這一行再填自己的資料
    ws.append([
        "2026-06-25", "陳令佳", "所羅門股份有限公司", "AB",
        "1769-IQ32", "", 10, "蔡培君", "20260320004", "倉庫右側",
    ])
    widths = [12, 10, 24, 12, 22, 20, 8, 10, 16, 14]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_fig1(file_bytes: bytes) -> list[dict]:
    """讀第一個 sheet（預設），回傳 dict 列表。空列已過濾。"""
    from io import BytesIO
    wb = load_workbook(BytesIO(file_bytes), data_only=True)
    if not wb.sheetnames:
        raise ValueError("Excel 內沒有任何 sheet")
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [_norm(x) for x in rows[0]]
    # 容錯：允許欄位順序不同，按表頭名稱對應
    idx = {h: header.index(h) if h in header else None for h in FIG1_HEADERS}
    # 供應商欄位接受 "供應商" 或舊名 "出貨對象"
    if idx["供應商"] is None:
        for alias in SUPPLIER_ALIASES:
            if alias in header:
                idx["供應商"] = header.index(alias)
                break
    missing = [h for h, v in idx.items() if v is None and h in
               ("到貨日期", "品牌", "產品名稱", "進貨數量")]
    if missing:
        raise ValueError(f"缺少必要欄位：{', '.join(missing)}")

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
            "supplier": _norm(cell("供應商")),
            "brand": _norm(cell("品牌")),
            "model": _norm(cell("產品名稱")),
            "serials": _parse_serials(cell("序號")),
            "qty": _parse_qty(cell("進貨數量")),
            "requester": _norm(cell("請購人員")),
            "po_no": _norm(cell("請購PO")),
            "location": _norm(cell("存放位置")),
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
        if msgs:
            errors.append({"row": r["row_no"], "msgs": msgs})
            continue
        valid_rows.append(r)

    # 依 (date, po_no, signer, supplier) 分組
    groups: dict[tuple, list[dict]] = {}
    for r in valid_rows:
        key = (r["date"], r["po_no"], r["signer"], r["supplier"])
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
        for (d, po, sg, sup), lines in groups.items():
            stats["details"].append({
                "date": d, "po_no": po, "signer": sg, "supplier": sup, "lines": len(lines),
                "preview": [f'{ln["brand"]} {ln["model"]} x{ln["qty"]} → {ln["location"]}' for ln in lines],
            })
        return stats

    with db.tx() as c:
        for (d, po_no, signer, supplier), lines in groups.items():
            # 有 PO 才檢查重複；無 PO 一律當新單寫入
            if po_no:
                existing = c.execute("""SELECT io.id FROM inbound_orders io
                                        LEFT JOIN purchase_orders po ON po.id=io.po_id
                                        WHERE po.po_no=?""", (po_no,)).fetchone()
                if existing:
                    stats["skipped_existing_po"] += 1
                    stats["details"].append({
                        "date": d, "po_no": po_no, "signer": signer, "supplier": supplier,
                        "lines": len(lines), "action": "skipped (PO 已存在)",
                    })
                    continue

            signer_id = _get_or_create(c, "staff", "name", signer, {"role": "簽收"}) if signer else None
            supplier_id = _get_or_create(c, "suppliers", "name", supplier) if supplier else None
            # 用同一個請購人員（取群組第一行）
            requester_name = next((ln["requester"] for ln in lines if ln["requester"]), "")
            requester_id = _get_or_create(c, "staff", "name", requester_name, {"role": "請購"}) if requester_name else None

            # PO 主檔（PO 為空則不建）
            po_id = None
            if po_no:
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

            cur = c.execute("""INSERT INTO inbound_orders(type, date, supplier_id, signer_id, po_id, note)
                               VALUES('hsinchu', ?, ?, ?, ?, ?)""",
                            (d, supplier_id, signer_id, po_id, None))
            in_id = cur.lastrowid
            stats["groups_inserted"] += 1

            for ln in lines:
                brand_id = _get_or_create(c, "brands", "name", ln["brand"])
                prod_id = _get_or_create_product(c, brand_id, ln["model"], "")
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
                "date": d, "po_no": po_no, "signer": signer, "supplier": supplier,
                "lines": len(lines), "action": "imported",
            })

    return stats
