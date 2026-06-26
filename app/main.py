from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from typing import Optional, List
import json

from . import db
from . import importer

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="倉管系統")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.on_event("startup")
def _startup():
    db.init_db()
    print(f"[DB] using database file: {db.DB_PATH}", flush=True)
    n = fetch_one("SELECT COUNT(*) AS n FROM products")["n"]
    print(f"[DB] products in store: {n}", flush=True)


def render(request: Request, tpl: str, **ctx):
    from datetime import date as _date
    ctx["request"] = request
    ctx.setdefault("today", _date.today().isoformat())
    return templates.TemplateResponse(tpl, ctx)


def fetch_all(sql, params=()):
    with db.tx() as c:
        return c.execute(sql, params).fetchall()


def fetch_one(sql, params=()):
    with db.tx() as c:
        return c.execute(sql, params).fetchone()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def get_or_create_location(c, code: str):
    code = (code or "").strip()
    if not code:
        return None
    row = c.execute("SELECT id FROM locations WHERE code=?", (code,)).fetchone()
    if row:
        return row["id"]
    cur = c.execute("INSERT INTO locations(code) VALUES(?)", (code,))
    return cur.lastrowid


def lookup_location_id(c, code: str):
    code = (code or "").strip()
    if not code:
        return None
    row = c.execute("SELECT id FROM locations WHERE code=?", (code,)).fetchone()
    return row["id"] if row else None


def safe_delete(table: str, row_id: int, refs: list, label: str):
    """Try to delete; if FK refs exist, return user-friendly HTTPException."""
    with db.tx() as c:
        for ref_table, ref_col, ref_label in refs:
            n = c.execute(f"SELECT COUNT(*) n FROM {ref_table} WHERE {ref_col}=?", (row_id,)).fetchone()["n"]
            if n > 0:
                raise HTTPException(409, f"無法刪除此{label}：仍被 {n} 筆「{ref_label}」使用中。請先處理相關紀錄。")
        c.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))


# ---------- Dashboard ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    stats = {
        "products": fetch_one("SELECT COUNT(*) n FROM products")["n"],
        "projects": fetch_one("SELECT COUNT(*) n FROM projects")["n"],
        "inbound": fetch_one("SELECT COUNT(*) n FROM inbound_orders")["n"],
        "outbound": fetch_one("SELECT COUNT(*) n FROM outbound_orders")["n"],
        "serials_in": fetch_one("SELECT COUNT(*) n FROM serial_items WHERE status='in_stock'")["n"],
        "loans_open": fetch_one("SELECT COUNT(*) n FROM loans WHERE status='out'")["n"],
        "photo_pending": fetch_one("SELECT COUNT(*) n FROM inbound_orders WHERE photo_sent=0")["n"],
    }
    low = fetch_all("""
      SELECT p.id, b.name brand, p.model, p.description, p.safety_stock,
             COALESCE((SELECT SUM(qty) FROM stock_balance WHERE product_id=p.id),0) qty
      FROM products p LEFT JOIN brands b ON b.id=p.brand_id
      WHERE p.safety_stock > 0
    """)
    low = [r for r in low if r["qty"] < r["safety_stock"]]
    return render(request, "index.html", stats=stats, low=low)


# ---------- 主檔 ----------
def _simple_master(name, table, cols):
    @app.get(f"/{name}", response_class=HTMLResponse, name=f"{name}_list")
    def _list(request: Request):
        rows = fetch_all(f"SELECT * FROM {table} ORDER BY id DESC")
        return render(request, "master_list.html", title=name, table=table, cols=cols, rows=rows)

    @app.post(f"/{name}/new")
    def _new(**form):
        pass


# 品牌
@app.get("/brands", response_class=HTMLResponse)
def brands_list(request: Request):
    rows = fetch_all("SELECT * FROM brands ORDER BY name")
    return render(request, "brands.html", rows=rows)


@app.post("/brands/new")
def brands_new(name: str = Form(...)):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO brands(name) VALUES(?)", (name.strip(),))
    return RedirectResponse("/brands", 303)


@app.post("/brands/{bid}/del")
def brands_del(bid: int):
    safe_delete("brands", bid, [("products", "brand_id", "料件")], "品牌")
    return RedirectResponse("/brands", 303)


# 供應商
@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_list(request: Request):
    rows = fetch_all("SELECT * FROM suppliers ORDER BY name")
    return render(request, "suppliers.html", rows=rows)


@app.post("/suppliers/new")
def suppliers_new(name: str = Form(...)):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO suppliers(name) VALUES(?)", (name.strip(),))
    return RedirectResponse("/suppliers", 303)


@app.post("/suppliers/{i}/del")
def suppliers_del(i: int):
    safe_delete("suppliers", i, [("inbound_orders", "supplier_id", "進貨單")], "供應商")
    return RedirectResponse("/suppliers", 303)


# 人員
@app.get("/staff", response_class=HTMLResponse)
def staff_list(request: Request):
    rows = fetch_all("SELECT * FROM staff ORDER BY name")
    return render(request, "staff.html", rows=rows)


@app.post("/staff/new")
def staff_new(name: str = Form(...), role: str = Form("")):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO staff(name, role) VALUES(?,?)", (name.strip(), role.strip()))
    return RedirectResponse("/staff", 303)


@app.post("/staff/{i}/del")
def staff_del(i: int):
    safe_delete("staff", i, [
        ("inbound_orders", "signer_id", "進貨單(簽收)"),
        ("outbound_orders", "signer_id", "出貨單(簽收)"),
        ("outbound_orders", "notifier_id", "出貨單(通知)"),
        ("purchase_orders", "requester_id", "請購單"),
    ], "人員")
    return RedirectResponse("/staff", 303)


# 存放位置
@app.get("/locations", response_class=HTMLResponse)
def loc_list(request: Request):
    rows = fetch_all("SELECT * FROM locations ORDER BY code")
    return render(request, "locations.html", rows=rows)


@app.post("/locations/new")
def loc_new(code: str = Form(...), name: str = Form("")):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO locations(code, name) VALUES(?,?)", (code.strip(), name.strip()))
    return RedirectResponse("/locations", 303)


@app.post("/locations/{i}/del")
def loc_del(i: int):
    safe_delete("locations", i, [
        ("inbound_lines", "location_id", "進貨明細"),
        ("outbound_lines", "from_location_id", "出貨明細"),
        ("serial_items", "current_location_id", "序號"),
    ], "位置")
    return RedirectResponse("/locations", 303)


# 工號 / 案件
@app.get("/projects", response_class=HTMLResponse)
def proj_list(request: Request, q: str = "", owner: str = ""):
    where, params = [], []
    if q:
        where.append("(job_no LIKE ? OR owner LIKE ? OR project_name LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if owner:
        where.append("owner = ?")
        params.append(owner)
    sql = "SELECT * FROM projects"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY job_no DESC"
    rows = fetch_all(sql, params)
    owners = [r["owner"] for r in fetch_all(
        "SELECT DISTINCT owner FROM projects WHERE owner IS NOT NULL AND owner <> '' ORDER BY owner")]
    return render(request, "projects.html", rows=rows, q=q, owner=owner, owners=owners)


@app.post("/projects/new")
def proj_new(job_no: str = Form(...), owner: str = Form(""), project_name: str = Form("")):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO projects(job_no, owner, project_name) VALUES(?,?,?)",
                  (job_no.strip(), owner.strip(), project_name.strip()))
    return RedirectResponse("/projects", 303)


@app.get("/projects/{i}/edit", response_class=HTMLResponse)
def proj_edit_form(request: Request, i: int):
    p = fetch_one("SELECT * FROM projects WHERE id=?", (i,))
    if not p:
        raise HTTPException(404)
    return render(request, "project_edit.html", p=p)


@app.post("/projects/{i}/edit")
def proj_edit_post(i: int, job_no: str = Form(...), owner: str = Form(""),
                   project_name: str = Form("")):
    import sqlite3
    try:
        with db.tx() as c:
            c.execute("UPDATE projects SET job_no=?, owner=?, project_name=? WHERE id=?",
                      (job_no.strip(), owner.strip() or None, project_name.strip() or None, i))
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"工號「{job_no}」已存在於其他列。")
        raise
    return RedirectResponse(f"/projects/{i}", 303)


@app.post("/projects/{i}/del")
def proj_del(i: int):
    safe_delete("projects", i, [
        ("inbound_orders", "project_id", "進貨單"),
        ("outbound_orders", "project_id", "出貨單"),
    ], "工號")
    return RedirectResponse("/projects", 303)


@app.get("/projects/import", response_class=HTMLResponse)
def projects_import_form(request: Request):
    return render(request, "projects_import.html", result=None)


@app.post("/projects/import", response_class=HTMLResponse)
async def projects_import_post(request: Request, file: UploadFile = File(...),
                               dry_run: int = Form(0)):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "請上傳 .xlsx 檔")
    data = await file.read()
    try:
        result = importer.import_projects(data, dry_run=bool(dry_run))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return render(request, "projects_import.html", result=result,
                  dry_run=bool(dry_run), filename=file.filename)


@app.get("/projects/import/template")
def projects_template():
    from fastapi.responses import Response
    data = importer.build_projects_template()
    return Response(content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="projects_template.xlsx"'})


@app.get("/projects/{i}", response_class=HTMLResponse)
def proj_detail(request: Request, i: int):
    p = fetch_one("SELECT * FROM projects WHERE id=?", (i,))
    if not p:
        raise HTTPException(404)
    inbound = fetch_all("""
      SELECT io.id, io.date, io.type, b.name brand, p.model, p.description,
             il.qty, il.unit, l.code loc
      FROM inbound_lines il
      JOIN inbound_orders io ON io.id=il.inbound_id
      JOIN products p ON p.id=il.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=il.location_id
      WHERE io.project_id=? ORDER BY io.date DESC
    """, (i,))
    outbound = fetch_all("""
      SELECT oo.id, oo.date, oo.type, b.name brand, p.model, p.description,
             ol.qty, ol.unit, l.code loc, ol.from_surplus
      FROM outbound_lines ol
      JOIN outbound_orders oo ON oo.id=ol.outbound_id
      JOIN products p ON p.id=ol.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=ol.from_location_id
      WHERE oo.project_id=? ORDER BY oo.date DESC
    """, (i,))
    return render(request, "project_detail.html", p=p, inbound=inbound, outbound=outbound)


# 料件
@app.get("/products", response_class=HTMLResponse)
def prod_list(request: Request, q: str = ""):
    where = ""
    params = ()
    if q:
        where = "WHERE p.model LIKE ? OR p.description LIKE ? OR b.name LIKE ?"
        params = (f"%{q}%", f"%{q}%", f"%{q}%")
    rows = fetch_all(f"""
      SELECT p.*, b.name brand,
        COALESCE((SELECT SUM(qty) FROM stock_balance WHERE product_id=p.id),0) qty,
        COALESCE((SELECT SUM(qty) FROM stock_balance WHERE product_id=p.id AND is_surplus=1),0) qty_surplus
      FROM products p LEFT JOIN brands b ON b.id=p.brand_id
      {where} ORDER BY b.name, p.model
    """, params)
    brands = fetch_all("SELECT * FROM brands ORDER BY name")
    return render(request, "products.html", rows=rows, brands=brands, q=q)


@app.get("/products/import", response_class=HTMLResponse)
def parts_import_form(request: Request):
    return render(request, "parts_import.html", result=None)


@app.post("/products/import", response_class=HTMLResponse)
async def parts_import_post(request: Request, file: UploadFile = File(...),
                            dry_run: int = Form(0)):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "請上傳 .xlsx 檔")
    data = await file.read()
    try:
        result = importer.import_parts(data, dry_run=bool(dry_run))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return render(request, "parts_import.html", result=result,
                  dry_run=bool(dry_run), filename=file.filename)


@app.get("/products/import/template")
def parts_template():
    from fastapi.responses import Response
    data = importer.build_parts_template()
    return Response(content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="parts_template.xlsx"'})


@app.get("/products/new", response_class=HTMLResponse)
def prod_new_form(request: Request):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO brands(name) VALUES('Others')")
    brands = fetch_all("SELECT * FROM brands ORDER BY (name='Others'), name")
    products = rows_to_dicts(fetch_all("""
      SELECT p.id, p.model, p.description, p.is_kit, b.name AS brand
      FROM products p LEFT JOIN brands b ON b.id=p.brand_id
      ORDER BY b.name, p.model
    """))
    return render(request, "product_new.html", brands=brands, products=products)


@app.post("/products/new")
async def prod_new(request: Request):
    import sqlite3
    form = await request.form()
    brand_id = int(form.get("brand_id"))
    model = (form.get("model") or "").strip()
    description = (form.get("description") or "").strip()
    base_unit = (form.get("base_unit") or "個").strip()
    track_by_serial = 1 if form.get("track_by_serial") else 0
    safety_stock = float(form.get("safety_stock") or 0)
    is_kit = 1 if form.get("is_kit") else 0
    if not model:
        raise HTTPException(400, "型號為必填")
    comp_ids = form.getlist("kit_component_id")
    comp_qtys = form.getlist("kit_component_qty")
    valid_components = []
    if is_kit:
        for idx, cid in enumerate(comp_ids):
            if not cid:
                continue
            try:
                q = float(comp_qtys[idx] or 0)
            except (TypeError, ValueError):
                q = 0
            if q <= 0:
                continue
            valid_components.append((int(cid), q))
        if not valid_components:
            raise HTTPException(400, "組合件至少要加入一個元件 (含數量 > 0)")
    try:
        with db.tx() as c:
            cur = c.execute("""INSERT INTO products
                (brand_id, model, description, base_unit, track_by_serial, safety_stock, is_kit)
                VALUES(?,?,?,?,?,?,?)""",
                (brand_id, model, description, base_unit, track_by_serial, safety_stock, is_kit))
            pid = cur.lastrowid
            for cid, q in valid_components:
                # 拒絕巢狀組合
                child = c.execute("SELECT is_kit FROM products WHERE id=?", (cid,)).fetchone()
                if child and child["is_kit"]:
                    raise HTTPException(400, "元件本身不可以是組合件 (暫不支援巢狀)")
                if cid == pid:
                    raise HTTPException(400, "元件不可以是自己")
                c.execute("""INSERT INTO kit_components(parent_product_id, component_product_id, qty)
                             VALUES(?,?,?)""", (pid, cid, q))
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            with db.tx() as c:
                bn = c.execute("SELECT name FROM brands WHERE id=?", (brand_id,)).fetchone()
            brand_name = bn["name"] if bn else f"brand_id={brand_id}"
            raise HTTPException(409, f"料件「{brand_name} {model}」已存在，請改用編輯功能修改其資料。")
        raise
    return RedirectResponse(f"/products/{pid}", 303)


@app.get("/products/{i}/edit", response_class=HTMLResponse)
def prod_edit_form(request: Request, i: int):
    p = fetch_one("SELECT * FROM products WHERE id=?", (i,))
    if not p:
        raise HTTPException(404)
    brands = fetch_all("SELECT * FROM brands ORDER BY (name='Others'), name")
    return render(request, "product_edit.html", p=p, brands=brands)


@app.post("/products/{i}/edit")
def prod_edit_post(i: int, brand_id: int = Form(...), model: str = Form(...),
                   description: str = Form(""), base_unit: str = Form("個"),
                   track_by_serial: int = Form(0), safety_stock: float = Form(0)):
    with db.tx() as c:
        c.execute("""UPDATE products SET brand_id=?, model=?, description=?, base_unit=?,
                     track_by_serial=?, safety_stock=? WHERE id=?""",
                  (brand_id, model.strip(), description.strip(), base_unit.strip(),
                   1 if track_by_serial else 0, safety_stock, i))
    return RedirectResponse(f"/products/{i}", 303)


@app.post("/products/{i}/comment")
def prod_comment(i: int, comment: str = Form("")):
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM products WHERE id=?", (i,)).fetchone():
            raise HTTPException(404)
        c.execute("UPDATE products SET comment=? WHERE id=?",
                  (comment.strip() or None, i))
    return RedirectResponse(f"/products/{i}", 303)


@app.post("/products/{i}/del")
def prod_del(i: int):
    safe_delete("products", i, [
        ("inbound_lines", "product_id", "進貨明細"),
        ("outbound_lines", "product_id", "出貨明細"),
        ("serial_items", "product_id", "序號"),
    ], "料件")
    return RedirectResponse("/products", 303)


@app.get("/products/{i}", response_class=HTMLResponse)
def prod_detail(request: Request, i: int):
    p = fetch_one("""SELECT p.*, b.name brand FROM products p LEFT JOIN brands b ON b.id=p.brand_id WHERE p.id=?""", (i,))
    if not p:
        raise HTTPException(404)
    stock = fetch_all("""
      SELECT l.code loc, sb.is_surplus, sb.qty
      FROM stock_balance sb LEFT JOIN locations l ON l.id=sb.location_id
      WHERE sb.product_id=? AND sb.qty<>0
    """, (i,))
    inbound = fetch_all("""
      SELECT io.id, io.date, io.type, il.qty, il.unit, l.code loc, il.is_surplus
      FROM inbound_lines il JOIN inbound_orders io ON io.id=il.inbound_id
      LEFT JOIN locations l ON l.id=il.location_id
      WHERE il.product_id=? ORDER BY io.date DESC
    """, (i,))
    outbound = fetch_all("""
      SELECT oo.id, oo.date, oo.type, ol.qty, ol.unit, l.code loc, ol.from_surplus,
             pj.job_no
      FROM outbound_lines ol JOIN outbound_orders oo ON oo.id=ol.outbound_id
      LEFT JOIN locations l ON l.id=ol.from_location_id
      LEFT JOIN projects pj ON pj.id=oo.project_id
      WHERE ol.product_id=? ORDER BY oo.date DESC
    """, (i,))
    serials = fetch_all("""
      SELECT s.*, l.code loc FROM serial_items s LEFT JOIN locations l ON l.id=s.current_location_id
      WHERE s.product_id=? ORDER BY s.serial_no
    """, (i,))
    kit_components = fetch_all("""
      SELECT kc.qty, cp.id cid, cp.model, cp.description, b.name brand
      FROM kit_components kc
      JOIN products cp ON cp.id=kc.component_product_id
      LEFT JOIN brands b ON b.id=cp.brand_id
      WHERE kc.parent_product_id=? ORDER BY b.name, cp.model
    """, (i,))
    used_in_kits = fetch_all("""
      SELECT kc.qty, pp.id pid, pp.model, pp.description, b.name brand
      FROM kit_components kc
      JOIN products pp ON pp.id=kc.parent_product_id
      LEFT JOIN brands b ON b.id=pp.brand_id
      WHERE kc.component_product_id=? ORDER BY b.name, pp.model
    """, (i,))
    return render(request, "product_detail.html", p=p, stock=stock, inbound=inbound,
                  outbound=outbound, serials=serials,
                  kit_components=kit_components, used_in_kits=used_in_kits)


# ---------- 進貨 ----------
@app.get("/inbound", response_class=HTMLResponse)
def in_list(request: Request, pending: int = 0, job_no: str = ""):
    clauses, params = [], []
    if pending:
        clauses.append("io.photo_sent=0")
    if job_no:
        clauses.append("p.job_no=?")
        params.append(job_no)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = fetch_all(f"""
      SELECT io.*, s.name supplier, st.name signer, p.job_no, po.po_no,
             rq.name requester,
             (SELECT COUNT(*) FROM inbound_lines WHERE inbound_id=io.id) lines
      FROM inbound_orders io
      LEFT JOIN suppliers s ON s.id=io.supplier_id
      LEFT JOIN staff st ON st.id=io.signer_id
      LEFT JOIN projects p ON p.id=io.project_id
      LEFT JOIN purchase_orders po ON po.id=io.po_id
      LEFT JOIN staff rq ON rq.id=po.requester_id
      {where}
      ORDER BY io.id DESC
    """, tuple(params))
    job_nos = fetch_all("""
      SELECT DISTINCT p.job_no FROM inbound_orders io
      JOIN projects p ON p.id=io.project_id
      WHERE p.job_no IS NOT NULL AND p.job_no<>''
      ORDER BY p.job_no DESC
    """)
    return render(request, "inbound_list.html", rows=rows, pending=pending,
                  job_no=job_no, job_nos=job_nos)


def _back(request: Request, default: str):
    return request.headers.get("referer") or default


@app.post("/inbound/{i}/photo_sent")
def in_photo_sent(request: Request, i: int, date: str = Form("")):
    from datetime import date as _date
    d = date or _date.today().isoformat()
    with db.tx() as c:
        c.execute("UPDATE inbound_orders SET photo_sent=1, photo_sent_date=? WHERE id=?", (d, i))
    return RedirectResponse(_back(request, "/inbound"), 303)


@app.post("/inbound/{i}/photo_na")
def in_photo_na(request: Request, i: int):
    with db.tx() as c:
        c.execute("UPDATE inbound_orders SET photo_sent=2, photo_sent_date=NULL WHERE id=?", (i,))
    return RedirectResponse(_back(request, "/inbound"), 303)


@app.get("/inbound/new", response_class=HTMLResponse)
def in_new_form(request: Request, type: str = "hsinchu"):
    if type not in ("hsinchu", "office", "surplus_return"):
        raise HTTPException(400, "invalid type")
    ctx = {
        "type": type,
        "suppliers": fetch_all("SELECT * FROM suppliers ORDER BY name"),
        "staff": fetch_all("SELECT * FROM staff ORDER BY name"),
        "requesters": fetch_all("SELECT * FROM staff WHERE role='請購' ORDER BY name"),
        "projects": fetch_all("SELECT * FROM projects ORDER BY job_no DESC"),
        "products": rows_to_dicts(fetch_all("SELECT p.*, b.name brand FROM products p LEFT JOIN brands b ON b.id=p.brand_id WHERE p.is_kit=0 ORDER BY b.name, p.model")),
        "locations": rows_to_dicts(fetch_all("SELECT * FROM locations ORDER BY code")),
        "outbound_lines": rows_to_dicts(fetch_all("""
            SELECT ol.id, oo.date, p.model, b.name brand, ol.qty, pj.job_no
            FROM outbound_lines ol
            JOIN outbound_orders oo ON oo.id=ol.outbound_id
            JOIN products p ON p.id=ol.product_id
            LEFT JOIN brands b ON b.id=p.brand_id
            LEFT JOIN projects pj ON pj.id=oo.project_id
            ORDER BY oo.date DESC LIMIT 200
        """)) if type == "surplus_return" else [],
    }
    return render(request, "inbound_form.html", **ctx)


@app.post("/inbound/new")
async def in_new_post(request: Request):
    form = await request.form()
    t = form.get("type")
    if t not in ("hsinchu", "office", "surplus_return"):
        raise HTTPException(400)
    with db.tx() as c:
        po_id = None
        if t == "hsinchu" and form.get("po_no"):
            req_id = int(form.get("requester_id")) if form.get("requester_id") else None
            c.execute("INSERT OR IGNORE INTO purchase_orders(po_no, date, requester_id) VALUES(?,?,?)",
                      (form.get("po_no").strip(), form.get("date"), req_id))
            row = c.execute("SELECT id FROM purchase_orders WHERE po_no=?", (form.get("po_no").strip(),)).fetchone()
            po_id = row["id"] if row else None
            if po_id and req_id:
                c.execute("UPDATE purchase_orders SET requester_id=? WHERE id=? AND requester_id IS NULL",
                          (req_id, po_id))
        cur = c.execute("""INSERT INTO inbound_orders(type, date, supplier_id, signer_id, po_id, project_id, note)
                           VALUES(?,?,?,?,?,?,?)""",
                        (t, form.get("date"),
                         int(form.get("supplier_id")) if form.get("supplier_id") else None,
                         int(form.get("signer_id")) if form.get("signer_id") else None,
                         po_id,
                         int(form.get("project_id")) if form.get("project_id") else None,
                         form.get("note", "")))
        in_id = cur.lastrowid

        # 解析多筆 line
        product_ids = form.getlist("line_product_id")
        qtys = form.getlist("line_qty")
        units = form.getlist("line_unit")
        loc_codes = form.getlist("line_location_code")
        sources = form.getlist("line_source_outbound_line_id")
        serials_json = form.getlist("line_serials")
        is_surplus_flag = 1 if t == "surplus_return" else 0

        for idx, pid in enumerate(product_ids):
            if not pid:
                continue
            qty = float(qtys[idx] or 0)
            if qty <= 0:
                continue
            loc_id = get_or_create_location(c, loc_codes[idx] if idx < len(loc_codes) else "")
            src = None
            if t == "surplus_return" and idx < len(sources) and sources[idx]:
                src = int(sources[idx])
            cur2 = c.execute("""INSERT INTO inbound_lines
                (inbound_id, product_id, qty, unit, location_id, is_surplus, source_outbound_line_id)
                VALUES(?,?,?,?,?,?,?)""",
                (in_id, int(pid), qty, units[idx] if idx < len(units) else None,
                 loc_id, is_surplus_flag, src))
            line_id = cur2.lastrowid
            # 序號處理
            sns_raw = serials_json[idx] if idx < len(serials_json) else ""
            sns = [s.strip() for s in sns_raw.replace(",", "\n").splitlines() if s.strip()]
            for sn in sns:
                if t == "surplus_return":
                    # 試圖將既有序號標回入庫
                    existing = c.execute("SELECT id FROM serial_items WHERE product_id=? AND serial_no=?",
                                          (int(pid), sn)).fetchone()
                    if existing:
                        c.execute("""UPDATE serial_items SET status='returned', is_surplus=1,
                                     current_location_id=?, inbound_line_id=?
                                     WHERE id=?""", (loc_id, line_id, existing["id"]))
                    else:
                        c.execute("""INSERT INTO serial_items(product_id, serial_no, status, current_location_id,
                                     inbound_line_id, is_surplus) VALUES(?,?,?,?,?,1)""",
                                  (int(pid), sn, "returned", loc_id, line_id))
                else:
                    c.execute("""INSERT OR IGNORE INTO serial_items(product_id, serial_no, status,
                                 current_location_id, inbound_line_id, is_surplus)
                                 VALUES(?,?,?,?,?,?)""",
                              (int(pid), sn, "in_stock", loc_id, line_id, 0))
    return RedirectResponse(f"/inbound/{in_id}", 303)


@app.get("/inbound/{i}", response_class=HTMLResponse)
def in_detail(request: Request, i: int):
    head = fetch_one("""
      SELECT io.*, s.name supplier, st.name signer, p.job_no, po.po_no, rq.name requester
      FROM inbound_orders io
      LEFT JOIN suppliers s ON s.id=io.supplier_id
      LEFT JOIN staff st ON st.id=io.signer_id
      LEFT JOIN projects p ON p.id=io.project_id
      LEFT JOIN purchase_orders po ON po.id=io.po_id
      LEFT JOIN staff rq ON rq.id=po.requester_id
      WHERE io.id=?
    """, (i,))
    if not head:
        raise HTTPException(404)
    lines = fetch_all("""
      SELECT il.*, b.name brand, p.model, p.description, l.code loc
      FROM inbound_lines il
      JOIN products p ON p.id=il.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=il.location_id
      WHERE il.inbound_id=?
    """, (i,))
    return render(request, "inbound_detail.html", h=head, lines=lines)


@app.get("/inbound/{i}/edit", response_class=HTMLResponse)
def in_edit_form(request: Request, i: int):
    h = fetch_one("""
      SELECT io.*, po.po_no, po.requester_id
      FROM inbound_orders io LEFT JOIN purchase_orders po ON po.id=io.po_id
      WHERE io.id=?
    """, (i,))
    if not h:
        raise HTTPException(404)
    ctx = {
        "h": h,
        "suppliers": fetch_all("SELECT * FROM suppliers ORDER BY name"),
        "staff": fetch_all("SELECT * FROM staff ORDER BY name"),
        "requesters": fetch_all("SELECT * FROM staff WHERE role='請購' ORDER BY name"),
        "projects": fetch_all("SELECT * FROM projects ORDER BY job_no DESC"),
    }
    return render(request, "inbound_edit.html", **ctx)


@app.post("/inbound/{i}/edit")
async def in_edit_post(request: Request, i: int):
    form = await request.form()
    date_v = form.get("date") or None
    supplier_id = int(form.get("supplier_id")) if form.get("supplier_id") else None
    signer_id = int(form.get("signer_id")) if form.get("signer_id") else None
    project_id = int(form.get("project_id")) if form.get("project_id") else None
    requester_id = int(form.get("requester_id")) if form.get("requester_id") else None
    po_no = (form.get("po_no") or "").strip()
    note = form.get("note", "")
    with db.tx() as c:
        head = c.execute("SELECT po_id FROM inbound_orders WHERE id=?", (i,)).fetchone()
        if not head:
            raise HTTPException(404)
        # PO: 三種情況 — 清空 / 沿用既有編號 / 換新編號
        if not po_no:
            new_po_id = None
        else:
            row = c.execute("SELECT id FROM purchase_orders WHERE po_no=?", (po_no,)).fetchone()
            if row:
                new_po_id = row["id"]
                c.execute("""UPDATE purchase_orders
                             SET requester_id=COALESCE(?, requester_id), date=COALESCE(date, ?)
                             WHERE id=?""", (requester_id, date_v, new_po_id))
            else:
                cur = c.execute("INSERT INTO purchase_orders(po_no, date, requester_id) VALUES(?,?,?)",
                                (po_no, date_v, requester_id))
                new_po_id = cur.lastrowid
        c.execute("""UPDATE inbound_orders
                     SET date=?, supplier_id=?, signer_id=?, project_id=?, po_id=?, note=?
                     WHERE id=?""",
                  (date_v, supplier_id, signer_id, project_id, new_po_id, note or None, i))
    return RedirectResponse(f"/inbound/{i}", 303)


@app.post("/inbound/{i}/note")
def in_note(i: int, note: str = Form("")):
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM inbound_orders WHERE id=?", (i,)).fetchone():
            raise HTTPException(404)
        c.execute("UPDATE inbound_orders SET note=? WHERE id=?",
                  (note.strip() or None, i))
    return RedirectResponse(f"/inbound/{i}", 303)


@app.post("/inbound/{i}/del")
def in_del(i: int):
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM inbound_orders WHERE id=?", (i,)).fetchone():
            raise HTTPException(404, "進貨單不存在")
        # 若任一序號已出貨，拒絕刪除以保留出貨歷史
        shipped = c.execute("""SELECT COUNT(*) n FROM serial_items si
                               JOIN inbound_lines il ON il.id = si.inbound_line_id
                               WHERE il.inbound_id=? AND si.status<>'in_stock'""", (i,)).fetchone()["n"]
        if shipped:
            raise HTTPException(409,
                f"此進貨單有 {shipped} 個序號已出貨/已退回，不可刪除（保留出貨歷史）")
        # 先移除仍在庫的序號 → inbound_lines 由 CASCADE 連動清除 → 最後刪單頭
        c.execute("""DELETE FROM serial_items
                     WHERE inbound_line_id IN
                       (SELECT id FROM inbound_lines WHERE inbound_id=?)""", (i,))
        c.execute("DELETE FROM inbound_orders WHERE id=?", (i,))
    return RedirectResponse("/inbound", 303)


# ---------- 出貨 ----------
@app.get("/outbound", response_class=HTMLResponse)
def out_list(request: Request):
    rows = fetch_all("""
      SELECT oo.*, sn.name notifier, sg.name signer, p.job_no, p.owner, p.project_name,
             (SELECT COUNT(*) FROM outbound_lines WHERE outbound_id=oo.id) lines
      FROM outbound_orders oo
      LEFT JOIN staff sn ON sn.id=oo.notifier_id
      LEFT JOIN staff sg ON sg.id=oo.signer_id
      LEFT JOIN projects p ON p.id=oo.project_id
      ORDER BY oo.id DESC
    """)
    return render(request, "outbound_list.html", rows=rows)


@app.get("/outbound/new", response_class=HTMLResponse)
def out_new_form(request: Request, type: str = "normal"):
    if type not in ("normal", "surplus_transfer"):
        raise HTTPException(400)
    # 取得每個 product 的庫存（依是否餘料分開）
    stock_rows = fetch_all("""
      SELECT sb.product_id, sb.location_id, sb.is_surplus, sb.qty,
             b.name brand, p.model, p.description, l.code loc
      FROM stock_balance sb
      JOIN products p ON p.id=sb.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=sb.location_id
      WHERE sb.qty<>0
      ORDER BY b.name, p.model
    """)
    products = rows_to_dicts(fetch_all("""
      SELECT p.*, b.name brand FROM products p
      LEFT JOIN brands b ON b.id=p.brand_id
      ORDER BY b.name, p.model
    """))
    # 組合件 BOM map for UI hint
    kit_rows = rows_to_dicts(fetch_all("""
      SELECT kc.parent_product_id, kc.qty, cp.id cid, cp.model component_model,
             b.name component_brand
      FROM kit_components kc
      JOIN products cp ON cp.id=kc.component_product_id
      LEFT JOIN brands b ON b.id=cp.brand_id
    """))
    ctx = {
        "type": type,
        "staff": fetch_all("SELECT * FROM staff ORDER BY name"),
        "projects": fetch_all("SELECT * FROM projects ORDER BY job_no DESC"),
        "products": products,
        "locations": rows_to_dicts(fetch_all("SELECT * FROM locations ORDER BY code")),
        "stock_rows": rows_to_dicts(stock_rows),
        "kit_rows": kit_rows,
    }
    return render(request, "outbound_form.html", **ctx)


@app.post("/outbound/new")
async def out_new_post(request: Request):
    form = await request.form()
    t = form.get("type")
    if t not in ("normal", "surplus_transfer"):
        raise HTTPException(400)
    from_surplus_flag = 1 if t == "surplus_transfer" else 0

    product_ids = form.getlist("line_product_id")
    qtys = form.getlist("line_qty")
    loc_codes = form.getlist("line_location_code")
    units = form.getlist("line_unit")
    sn_lists = form.getlist("line_serials")

    errors = []
    # plan: list of dicts {pid, qty, loc_id, unit, sns_raw, kit_parent_pid_or_None}
    plan = []

    def _prod_label(c, pid):
        p = c.execute("""SELECT p.model, b.name brand FROM products p
                         LEFT JOIN brands b ON b.id=p.brand_id WHERE p.id=?""", (pid,)).fetchone()
        return f"{p['brand'] or ''} {p['model']}" if p else f"#{pid}"

    with db.tx() as c:
        for idx, pid in enumerate(product_ids):
            if not pid:
                continue
            try:
                qty = float(qtys[idx] or 0)
            except ValueError:
                qty = 0
            if qty <= 0:
                continue
            pid = int(pid)
            unit_v = units[idx] if idx < len(units) else None
            sns_raw = sn_lists[idx] if idx < len(sn_lists) else ""
            row_no = idx + 1

            prow = c.execute("SELECT is_kit FROM products WHERE id=?", (pid,)).fetchone()
            if not prow:
                errors.append(f"第 {row_no} 行料件不存在")
                continue

            if prow["is_kit"]:
                # 組合件：忽略使用者填的位置，自動從庫存最多位置依序扣
                components = c.execute("""SELECT component_product_id, qty FROM kit_components
                                          WHERE parent_product_id=?""", (pid,)).fetchall()
                if not components:
                    errors.append(f"第 {row_no} 行 [{_prod_label(c, pid)}] 是組合件但未定義 BOM")
                    continue
                for comp in components:
                    cpid = comp["component_product_id"]
                    need = comp["qty"] * qty
                    locs = c.execute("""SELECT location_id, qty FROM stock_balance
                                        WHERE product_id=? AND is_surplus=? AND qty>0
                                        ORDER BY qty DESC""", (cpid, from_surplus_flag)).fetchall()
                    total_avail = sum(l["qty"] for l in locs)
                    if total_avail < need:
                        tag = "餘料" if from_surplus_flag else "正常"
                        errors.append(
                            f"第 {row_no} 行組合件 [{_prod_label(c, pid)}] 需要 "
                            f"[{_prod_label(c, cpid)}] x{need}，但{tag}庫存只有 {total_avail}"
                        )
                        continue
                    remaining = need
                    for l in locs:
                        if remaining <= 0:
                            break
                        take = min(l["qty"], remaining)
                        plan.append({
                            "pid": cpid, "qty": take, "loc_id": l["location_id"],
                            "unit": None, "sns_raw": "", "kit_parent_pid": pid,
                        })
                        remaining -= take
            else:
                # 普通料件：照舊路徑
                code = (loc_codes[idx] if idx < len(loc_codes) else "").strip()
                if not code:
                    errors.append(f"第 {row_no} 行未指定扣帳位置")
                    continue
                loc_id = lookup_location_id(c, code)
                if loc_id is None:
                    errors.append(f"第 {row_no} 行的位置「{code}」不存在於庫存")
                    continue
                row = c.execute("""SELECT qty FROM stock_balance
                                   WHERE product_id=? AND location_id=? AND is_surplus=?""",
                                (pid, loc_id, from_surplus_flag)).fetchone()
                avail = row["qty"] if row else 0
                if qty > avail:
                    loc = c.execute("SELECT code FROM locations WHERE id=?", (loc_id,)).fetchone()
                    tag = "餘料" if from_surplus_flag else "正常"
                    errors.append(f"第 {row_no} 行 [{_prod_label(c, pid)}] 在 [{loc['code']}] 的{tag}庫存只剩 {avail}，無法出貨 {qty}")
                    continue
                plan.append({
                    "pid": pid, "qty": qty, "loc_id": loc_id,
                    "unit": unit_v, "sns_raw": sns_raw, "kit_parent_pid": None,
                })

    if errors:
        raise HTTPException(400, "; ".join(errors))
    if not plan:
        raise HTTPException(400, "請至少加入一筆有效明細")

    # 額外檢查：跨明細同元件 + 同位置 + 同 surplus 的合計不可超過庫存
    # （單筆 line 上面已驗，但多筆都從同 location 扣可能超量）
    with db.tx() as c:
        totals = {}
        for ln in plan:
            key = (ln["pid"], ln["loc_id"], from_surplus_flag)
            totals[key] = totals.get(key, 0) + ln["qty"]
        for (pid, lid, sf), need in totals.items():
            row = c.execute("""SELECT qty FROM stock_balance
                               WHERE product_id=? AND location_id=? AND is_surplus=?""",
                            (pid, lid, sf)).fetchone()
            avail = row["qty"] if row else 0
            if need > avail:
                loc = c.execute("SELECT code FROM locations WHERE id=?", (lid,)).fetchone()
                raise HTTPException(400, f"[{_prod_label(c, pid)}] 在 [{loc['code']}] 的合計需求 {need} 超過庫存 {avail}")

        cur = c.execute("""INSERT INTO outbound_orders(type, date, notifier_id, recipient, signer_id,
                           sign_date, project_id, shipping_carrier, shipping_no, note)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (t, form.get("date"),
                         int(form.get("notifier_id")) if form.get("notifier_id") else None,
                         form.get("recipient", ""),
                         int(form.get("signer_id")) if form.get("signer_id") else None,
                         form.get("sign_date") or None,
                         int(form.get("project_id")) if form.get("project_id") else None,
                         form.get("shipping_carrier", ""),
                         form.get("shipping_no", ""),
                         form.get("note", "")))
        out_id = cur.lastrowid
        for ln in plan:
            note = None
            if ln["kit_parent_pid"]:
                note = f"組合件展開：{_prod_label(c, ln['kit_parent_pid'])}"
            cur2 = c.execute("""INSERT INTO outbound_lines(outbound_id, product_id, qty, unit,
                                from_location_id, from_surplus, note)
                                VALUES(?,?,?,?,?,?,?)""",
                             (out_id, ln["pid"], ln["qty"], ln["unit"],
                              ln["loc_id"], from_surplus_flag, note))
            line_id = cur2.lastrowid
            sns = [s.strip() for s in (ln["sns_raw"] or "").replace(",", "\n").splitlines() if s.strip()]
            for sn in sns:
                row = c.execute("SELECT id FROM serial_items WHERE product_id=? AND serial_no=?",
                                (ln["pid"], sn)).fetchone()
                if row:
                    c.execute("""UPDATE serial_items SET status='shipped',
                                 outbound_line_id=?, current_location_id=NULL WHERE id=?""",
                              (line_id, row["id"]))
                else:
                    c.execute("""INSERT INTO serial_items(product_id, serial_no, status, outbound_line_id)
                                 VALUES(?,?,?,?)""", (ln["pid"], sn, "shipped", line_id))
    return RedirectResponse(f"/outbound/{out_id}", 303)


@app.get("/outbound/{i}", response_class=HTMLResponse)
def out_detail(request: Request, i: int):
    head = fetch_one("""
      SELECT oo.*, sn.name notifier, sg.name signer, p.job_no, p.owner, p.project_name
      FROM outbound_orders oo
      LEFT JOIN staff sn ON sn.id=oo.notifier_id
      LEFT JOIN staff sg ON sg.id=oo.signer_id
      LEFT JOIN projects p ON p.id=oo.project_id WHERE oo.id=?
    """, (i,))
    if not head:
        raise HTTPException(404)
    lines = fetch_all("""
      SELECT ol.*, b.name brand, p.model, p.description, l.code loc
      FROM outbound_lines ol
      JOIN products p ON p.id=ol.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=ol.from_location_id
      WHERE ol.outbound_id=?
    """, (i,))
    return render(request, "outbound_detail.html", h=head, lines=lines)


@app.post("/outbound/{i}/del")
def out_del(i: int):
    with db.tx() as c:
        c.execute("DELETE FROM outbound_orders WHERE id=?", (i,))
    return RedirectResponse("/outbound", 303)


# ---------- 庫存 ----------
@app.get("/stock", response_class=HTMLResponse)
def stock(request: Request, q: str = "", only_surplus: int = 0):
    where = ["sb.qty<>0"]
    params = []
    if q:
        where.append("""(p.model LIKE ? OR p.description LIKE ? OR b.name LIKE ?
                         OR EXISTS (SELECT 1 FROM serial_items si
                                    WHERE si.product_id=p.id AND si.serial_no LIKE ?))""")
        params += [f"%{q}%"] * 4
    if only_surplus:
        where.append("sb.is_surplus=1")
    sql = f"""
      SELECT sb.product_id, b.name brand, p.model, p.description,
             l.code loc, sb.is_surplus, sb.qty, p.base_unit, p.safety_stock
      FROM stock_balance sb
      JOIN products p ON p.id=sb.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=sb.location_id
      WHERE {' AND '.join(where)}
      ORDER BY b.name, p.model, l.code
    """
    rows = fetch_all(sql, params)
    pending = fetch_all("""
      SELECT io.id, io.date, io.type, s.name supplier, p.job_no, po.po_no,
             (SELECT COUNT(*) FROM inbound_lines WHERE inbound_id=io.id) lines
      FROM inbound_orders io
      LEFT JOIN suppliers s ON s.id=io.supplier_id
      LEFT JOIN projects p ON p.id=io.project_id
      LEFT JOIN purchase_orders po ON po.id=io.po_id
      WHERE io.photo_sent=0
      ORDER BY io.date DESC
    """)
    return render(request, "stock.html", rows=rows, q=q, only_surplus=only_surplus, pending=pending)


# ---------- 序號追蹤 ----------
@app.get("/serials", response_class=HTMLResponse)
def serials(request: Request, q: str = ""):
    where = ""
    params = ()
    if q:
        where = "WHERE s.serial_no LIKE ? OR p.model LIKE ?"
        params = (f"%{q}%", f"%{q}%")
    rows = fetch_all(f"""
      SELECT s.*, b.name brand, p.model, p.description, l.code loc
      FROM serial_items s
      JOIN products p ON p.id=s.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=s.current_location_id
      {where} ORDER BY s.id DESC LIMIT 500
    """, params)
    return render(request, "serials.html", rows=rows, q=q)


@app.get("/serials/{sid}", response_class=HTMLResponse)
def serial_history(request: Request, sid: int):
    s = fetch_one("""
      SELECT s.*, b.name brand, p.model, p.description, l.code loc
      FROM serial_items s JOIN products p ON p.id=s.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=s.current_location_id
      WHERE s.id=?
    """, (sid,))
    if not s:
        raise HTTPException(404)
    inb = fetch_one("""SELECT io.*, l.code loc FROM inbound_lines il
                       JOIN inbound_orders io ON io.id=il.inbound_id
                       LEFT JOIN locations l ON l.id=il.location_id
                       WHERE il.id=?""", (s["inbound_line_id"],)) if s["inbound_line_id"] else None
    out = fetch_one("""SELECT oo.*, pj.job_no FROM outbound_lines ol
                       JOIN outbound_orders oo ON oo.id=ol.outbound_id
                       LEFT JOIN projects pj ON pj.id=oo.project_id
                       WHERE ol.id=?""", (s["outbound_line_id"],)) if s["outbound_line_id"] else None
    return render(request, "serial_detail.html", s=s, inb=inb, out=out)


# ---------- 借出管理 ----------
@app.get("/loans", response_class=HTMLResponse)
def loans_list(request: Request):
    rows = fetch_all("SELECT * FROM loans ORDER BY id DESC")
    return render(request, "loans.html", rows=rows)


@app.post("/loans/new")
def loans_new(loan_no: str = Form(...), borrower: str = Form(""), out_date: str = Form(""),
              note: str = Form("")):
    with db.tx() as c:
        c.execute("""INSERT OR IGNORE INTO loans(loan_no, borrower, out_date, note) VALUES(?,?,?,?)""",
                  (loan_no.strip(), borrower.strip(), out_date or None, note))
    return RedirectResponse("/loans", 303)


@app.post("/loans/{i}/return")
def loans_return(i: int, return_date: str = Form("")):
    with db.tx() as c:
        c.execute("UPDATE loans SET status='returned', return_date=? WHERE id=?",
                  (return_date or None, i))
    return RedirectResponse("/loans", 303)


@app.post("/loans/{i}/settle")
def loans_settle(i: int):
    with db.tx() as c:
        c.execute("UPDATE loans SET status='settled' WHERE id=?", (i,))
    return RedirectResponse("/loans", 303)


@app.post("/loans/{i}/del")
def loans_del(i: int):
    with db.tx() as c:
        c.execute("DELETE FROM loans WHERE id=?", (i,))
    return RedirectResponse("/loans", 303)


# ---------- Excel 匯入 ----------
@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    return render(request, "import.html", result=None)


@app.get("/import/template")
def import_template(t: str = "hsinchu"):
    from fastapi.responses import Response
    if t == "office":
        data = importer.build_office_template()
        fname = "office_inbound_template.xlsx"
    else:
        data = importer.build_fig1_template()
        fname = "inbound_template.xlsx"
    return Response(content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/import", response_class=HTMLResponse)
async def import_post(request: Request, file: UploadFile = File(...),
                      dry_run: int = Form(0)):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "請上傳 .xlsx 檔")
    data = await file.read()
    try:
        result = importer.import_inbound_auto(data, dry_run=bool(dry_run))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return render(request, "import.html", result=result,
                  dry_run=bool(dry_run), filename=file.filename)
