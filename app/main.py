from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from typing import Optional, List
import json
import uuid

from . import db
from . import importer

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
# 全域：整數值的 float（5.0）在模板中渲染為 5
templates.env.finalize = lambda v: int(v) if isinstance(v, float) and v.is_integer() else v

app = FastAPI(title="倉管系統")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.middleware("http")
async def _ensure_cart_session(request: Request, call_next):
    sid = request.cookies.get("wh_sess")
    new_sid = None
    if not sid:
        new_sid = uuid.uuid4().hex
        request.scope["wh_sess"] = new_sid
    else:
        request.scope["wh_sess"] = sid
    response = await call_next(request)
    if new_sid:
        response.set_cookie("wh_sess", new_sid, max_age=60 * 60 * 24 * 365,
                            httponly=False, samesite="lax")
    return response


def get_sess(request: Request) -> str:
    return request.scope.get("wh_sess") or request.cookies.get("wh_sess") or ""


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
    today = fetch_one("SELECT date('now','localtime') d")["d"]
    stats = {
        "products": fetch_one("SELECT COUNT(*) n FROM products")["n"],
        "projects": fetch_one("SELECT COUNT(*) n FROM projects")["n"],
        "inbound": fetch_one("SELECT COUNT(*) n FROM inbound_orders")["n"],
        "outbound": fetch_one("SELECT COUNT(*) n FROM outbound_orders")["n"],
        "serials_in": fetch_one("SELECT COUNT(*) n FROM serial_items WHERE status='in_stock'")["n"],
        "photo_pending": fetch_one("SELECT COUNT(*) n FROM inbound_orders WHERE photo_sent=0")["n"],
    }
    low = fetch_all("""
      SELECT p.id, b.name brand, p.model, p.description, p.safety_stock,
             COALESCE((SELECT SUM(qty) FROM stock_balance WHERE product_id=p.id),0) qty
      FROM products p LEFT JOIN brands b ON b.id=p.brand_id
      WHERE p.safety_stock > 0
    """)
    low = [r for r in low if r["qty"] < r["safety_stock"]]
    return render(request, "index.html", stats=stats, low=low, today=today)


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


@app.post("/suppliers/{i}/rename")
def suppliers_rename(i: int, name: str = Form(...)):
    new_name = name.strip()
    if not new_name:
        raise HTTPException(400, "名稱不可空白")
    with db.tx() as c:
        old = c.execute("SELECT name FROM suppliers WHERE id=?", (i,)).fetchone()
        if not old:
            raise HTTPException(404)
        if old["name"] == new_name:
            return RedirectResponse("/suppliers", 303)
        # 確認新名稱未與其他供應商衝突
        dup = c.execute("SELECT id FROM suppliers WHERE name=? AND id<>?",
                        (new_name, i)).fetchone()
        if dup:
            raise HTTPException(409, f"供應商「{new_name}」已存在")
        c.execute("UPDATE suppliers SET name=? WHERE id=?", (new_name, i))
        # 同步 Raw 校正區的 source 欄（已匯入 pending 列才動）
        c.execute("UPDATE raw_imports SET source=? WHERE source=? AND status<>'imported'",
                  (new_name, old["name"]))
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


@app.post("/staff/{i}/edit")
def staff_edit(i: int, name: str = Form(...), role: str = Form("")):
    new_name = name.strip()
    if not new_name:
        raise HTTPException(400, "姓名不可空白")
    with db.tx() as c:
        old = c.execute("SELECT name FROM staff WHERE id=?", (i,)).fetchone()
        if not old:
            raise HTTPException(404)
        # 重名檢查（與其他人員）
        dup = c.execute("SELECT id FROM staff WHERE name=? AND id<>?", (new_name, i)).fetchone()
        if dup:
            raise HTTPException(409, f"人員「{new_name}」已存在")
        c.execute("UPDATE staff SET name=?, role=? WHERE id=?",
                  (new_name, role.strip() or None, i))
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


@app.get("/projects/stock-overview", response_class=HTMLResponse)
def proj_stock_overview(request: Request):
    """跨工號 × 料件持有量總覽。project_id IS NULL → 自由池。"""
    rows = fetch_all("""
      SELECT sb.project_id,
             COALESCE(pj.job_no, '（自由池）') job_no,
             pj.owner, pj.project_name,
             COUNT(DISTINCT sb.product_id) product_kinds,
             SUM(CASE WHEN sb.is_surplus=0 THEN sb.qty ELSE 0 END) qty_normal,
             SUM(CASE WHEN sb.is_surplus=1 THEN sb.qty ELSE 0 END) qty_surplus,
             SUM(sb.qty) qty_total
      FROM stock_balance sb
      LEFT JOIN projects pj ON pj.id = sb.project_id
      WHERE sb.qty <> 0
      GROUP BY sb.project_id
      HAVING SUM(sb.qty) > 0
      ORDER BY (sb.project_id IS NULL), pj.job_no DESC
    """)
    return render(request, "projects_stock_overview.html", rows=rows)


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
    today = fetch_one("SELECT date('now','localtime') d")["d"]
    # 以型號為單位的彙整：依此工號的進貨 / 出貨 / 在庫量
    product_summary = fetch_all("""
      SELECT p.id product_id, b.name brand, p.model, p.description,
             p.base_unit unit,
             COALESCE(SUM(CASE WHEN src='in' THEN qty ELSE 0 END),0) inbound_qty,
             COALESCE(SUM(CASE WHEN src='surplus' THEN qty ELSE 0 END),0) surplus_qty,
             COALESCE(SUM(CASE WHEN src='out_ship' THEN qty ELSE 0 END),0) shipped_qty,
             COALESCE(SUM(CASE WHEN src='stock' THEN qty ELSE 0 END),0) in_stock_qty
      FROM (
        -- 進貨（正常）
        SELECT il.product_id, il.qty, 'in' src, il.is_surplus
        FROM inbound_lines il
        WHERE il.project_id=? AND il.is_surplus=0
        UNION ALL
        -- 進貨（餘料）— 以資訊揭露用
        SELECT il.product_id, il.qty, 'surplus' src, il.is_surplus
        FROM inbound_lines il
        WHERE il.project_id=? AND il.is_surplus=1
        UNION ALL
        -- 出貨：此工號作為「出貨工號」(oo.project_id)
        SELECT ol.product_id, ol.qty, 'out_ship' src, ol.from_surplus is_surplus
        FROM outbound_lines ol JOIN outbound_orders oo ON oo.id=ol.outbound_id
        WHERE oo.project_id=? AND oo.op_kind='ship'
        UNION ALL
        -- 在庫 (stock_balance)
        SELECT sb.product_id, sb.qty, 'stock' src, sb.is_surplus
        FROM stock_balance sb
        WHERE sb.project_id=? AND sb.qty <> 0
      ) sub
      JOIN products p ON p.id = sub.product_id
      LEFT JOIN brands b ON b.id = p.brand_id
      GROUP BY p.id
      HAVING (inbound_qty + surplus_qty + shipped_qty + in_stock_qty) > 0
      ORDER BY b.name, p.model
    """, (i, i, i, i))
    # 計算狀態徽章
    product_summary = [dict(r) for r in product_summary]
    for s in product_summary:
        total_in = float(s["inbound_qty"]) + float(s["surplus_qty"])
        in_stock = float(s["in_stock_qty"])
        shipped = float(s["shipped_qty"])
        if total_in > 0 and in_stock == total_in:
            s["state"] = "all_in_stock"
        elif total_in > 0 and shipped >= total_in:
            s["state"] = "all_shipped"
        elif in_stock == 0 and shipped > 0:
            s["state"] = "all_shipped"
        else:
            s["state"] = "partial"
    return render(request, "project_detail.html", p=p,
                  product_summary=product_summary,
                  inbound=inbound, outbound=outbound,
                  today=today)


@app.get("/projects/{pid}/product/{prod_id}/history")
def proj_product_history(pid: int, prod_id: int):
    """JSON 回傳指定工號 × 指定型號的進出貨歷史（含序號 / 業主 / 案名 / 交貨單頁數）。"""
    from fastapi.responses import JSONResponse
    # 有序號的 serial_items：被進貨歸於此工號 OR 出貨工號為此工號
    sers = fetch_all("""
      SELECT si.id, si.serial_no, si.status,
             il.inbound_id, io.date inbound_date, io.type inbound_type,
             il.is_surplus, il.page_no,
             ipj.job_no inbound_job, ipj.owner inbound_owner,
             ipj.project_name inbound_project_name,
             si.outbound_line_id, oo.id outbound_id, oo.date outbound_date,
             oo.type outbound_type, oo.op_kind out_op_kind,
             opj.job_no outbound_job, opj.owner outbound_owner,
             opj.project_name outbound_project_name
      FROM serial_items si
      LEFT JOIN inbound_lines il ON il.id = si.inbound_line_id
      LEFT JOIN inbound_orders io ON io.id = il.inbound_id
      LEFT JOIN projects ipj ON ipj.id = il.project_id
      LEFT JOIN outbound_lines ol ON ol.id = si.outbound_line_id
      LEFT JOIN outbound_orders oo ON oo.id = ol.outbound_id
      LEFT JOIN projects opj ON opj.id = oo.project_id
      WHERE si.product_id=?
        AND ( il.project_id=? OR oo.project_id=? )
    """, (prod_id, pid, pid))
    history = []
    for r in sers:
        d = dict(r)
        d["job_no"] = d["outbound_job"] or d["inbound_job"]
        d["owner"] = d["outbound_owner"] or d["inbound_owner"]
        d["project_name"] = d["outbound_project_name"] or d["inbound_project_name"]
        if d["outbound_id"]:
            d["state"] = "borrow" if d["out_op_kind"] == "borrow" else "shipped"
        elif d["status"] == "returned":
            d["state"] = "returned"
        else:
            d["state"] = "in_stock"
        history.append(d)
    # 非序號：以 FIFO 配對（限定此 project）
    in_rows = fetch_all("""
      SELECT il.id line_id, il.qty line_qty, il.is_surplus, il.page_no,
             io.id inbound_id, io.date inbound_date, io.type inbound_type,
             ipj.job_no inbound_job, ipj.owner inbound_owner,
             ipj.project_name inbound_project_name,
             (SELECT COUNT(*) FROM serial_items si
              WHERE si.inbound_line_id = il.id) sn_count
      FROM inbound_lines il
      JOIN inbound_orders io ON io.id = il.inbound_id
      LEFT JOIN projects ipj ON ipj.id = il.project_id
      WHERE il.product_id=? AND il.project_id=?
    """, (prod_id, pid))
    in_chunks = []
    for r in sorted([dict(x) for x in in_rows],
                     key=lambda x: (x["inbound_date"] or "", x["inbound_id"] or 0)):
        n = float(r["line_qty"]) - float(r["sn_count"] or 0)
        if n > 0:
            r["qty_left"] = n
            in_chunks.append(r)
    out_rows = fetch_all("""
      SELECT ol.id, ol.qty, ol.from_surplus is_surplus, ol.source_inbound_line_id,
             oo.id outbound_id, oo.date outbound_date, oo.type outbound_type,
             oo.op_kind out_op_kind,
             opj.job_no outbound_job, opj.owner outbound_owner,
             opj.project_name outbound_project_name
      FROM outbound_lines ol
      JOIN outbound_orders oo ON oo.id = ol.outbound_id
      LEFT JOIN projects opj ON opj.id = oo.project_id
      WHERE ol.product_id=? AND oo.project_id=?
        AND NOT EXISTS (SELECT 1 FROM serial_items si WHERE si.outbound_line_id=ol.id)
      ORDER BY oo.date ASC, oo.id ASC
    """, (prod_id, pid))
    def _next_src2(chunks, ob_date, src_il=None):
        if src_il:
            exp = next((c for c in chunks if c["qty_left"] > 0 and c.get("line_id") == src_il), None)
            if exp:
                return exp
        fifo = sorted([c for c in chunks if c["qty_left"] > 0],
                       key=lambda x: (x["inbound_date"] or "", x["inbound_id"] or 0))
        return fifo[0] if fifo else None
    for ob in out_rows:
        ob = dict(ob); remaining = float(ob["qty"])
        while remaining > 0:
            src = _next_src2(in_chunks, ob["outbound_date"], ob.get("source_inbound_line_id"))
            if not src:
                history.append({
                    "serial_no": None, "non_serial_qty": int(remaining),
                    "inbound_id": None, "inbound_date": None, "inbound_type": None,
                    "is_surplus": ob["is_surplus"], "page_no": None,
                    "outbound_id": ob["outbound_id"], "outbound_date": ob["outbound_date"],
                    "outbound_type": ob["outbound_type"], "out_op_kind": ob["out_op_kind"],
                    "outbound_job": ob["outbound_job"],
                    "job_no": ob["outbound_job"],
                    "owner": ob["outbound_owner"],
                    "project_name": ob["outbound_project_name"],
                    "state": ("borrow" if ob["out_op_kind"] == "borrow" else "shipped"),
                }); remaining = 0; break
            take = min(remaining, src["qty_left"])
            history.append({
                "serial_no": None, "non_serial_qty": int(take),
                "inbound_id": src["inbound_id"], "inbound_date": src["inbound_date"],
                "inbound_type": src["inbound_type"], "is_surplus": src["is_surplus"],
                "page_no": src["page_no"],
                "outbound_id": ob["outbound_id"], "outbound_date": ob["outbound_date"],
                "outbound_type": ob["outbound_type"], "out_op_kind": ob["out_op_kind"],
                "outbound_job": ob["outbound_job"],
                "job_no": ob["outbound_job"] or src["inbound_job"],
                "owner": ob["outbound_owner"] or src["inbound_owner"],
                "project_name": ob["outbound_project_name"] or src["inbound_project_name"],
                "state": ("borrow" if ob["out_op_kind"] == "borrow" else "shipped"),
            })
            src["qty_left"] -= take; remaining -= take
    for c in in_chunks:
        if c["qty_left"] <= 0: continue
        history.append({
            "serial_no": None, "non_serial_qty": int(c["qty_left"]),
            "inbound_id": c["inbound_id"], "inbound_date": c["inbound_date"],
            "inbound_type": c["inbound_type"], "is_surplus": c["is_surplus"],
            "page_no": c["page_no"],
            "outbound_id": None, "outbound_date": None,
            "outbound_type": None, "out_op_kind": None,
            "job_no": c["inbound_job"],
            "owner": c["inbound_owner"],
            "project_name": c["inbound_project_name"],
            "state": "in_stock_non",
        })
    def _key(x): return (x["outbound_date"] or "", x["inbound_date"] or "",
                          x.get("inbound_id") or 0)
    history.sort(key=_key, reverse=True)
    return JSONResponse({"history": history})


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
    if not model:
        raise HTTPException(400, "型號為必填")
    try:
        with db.tx() as c:
            cur = c.execute("""INSERT INTO products
                (brand_id, model, description, base_unit, track_by_serial, safety_stock, is_kit)
                VALUES(?,?,?,?,?,?,0)""",
                (brand_id, model, description, base_unit, track_by_serial, safety_stock))
            pid = cur.lastrowid
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            with db.tx() as c:
                bn = c.execute("SELECT name FROM brands WHERE id=?", (brand_id,)).fetchone()
            brand_name = bn["name"] if bn else f"brand_id={brand_id}"
            raise HTTPException(409, f"料件「{brand_name} {model}」已存在，請改用編輯功能修改其資料。")
        raise
    return RedirectResponse(f"/products/{pid}", 303)


@app.get("/products/{i}/edit", response_class=HTMLResponse)
def prod_edit_form(request: Request, i: int, q: str = ""):
    p = fetch_one("SELECT * FROM products WHERE id=?", (i,))
    if not p:
        raise HTTPException(404)
    brands = fetch_all("SELECT * FROM brands ORDER BY (name='Others'), name")
    return render(request, "product_edit.html", p=p, brands=brands, q=q)


@app.post("/products/{i}/edit")
def prod_edit_post(i: int, brand_id: Optional[str] = Form(None), model: str = Form(...),
                   description: str = Form(""), base_unit: str = Form("個"),
                   track_by_serial: int = Form(0), safety_stock: float = Form(0),
                   q: str = Form("")):
    new_model = model.strip()
    new_desc = description.strip() or None
    bid = int(brand_id) if (brand_id is not None and str(brand_id).strip() != "") else None
    with db.tx() as c:
        old = c.execute("SELECT model, description FROM products WHERE id=?", (i,)).fetchone()
        if not old:
            raise HTTPException(404)
        c.execute("""UPDATE products SET brand_id=?, model=?, description=?, base_unit=?,
                     track_by_serial=?, safety_stock=? WHERE id=?""",
                  (bid, new_model, new_desc or "", base_unit.strip(),
                   1 if track_by_serial else 0, safety_stock, i))
        # 同步：Raw 校正區的 model / description 一併更新（依舊 model 匹配）
        old_model = old["model"]
        old_desc = old["description"]
        if old_model and old_model != new_model:
            c.execute("UPDATE raw_imports SET model=? WHERE model=? AND status<>'imported'",
                      (new_model, old_model))
        # description 變更：對所有同 model 的 pending 列一併同步
        if (old_desc or "") != (new_desc or ""):
            c.execute("""UPDATE raw_imports SET description=?
                         WHERE model=? AND status<>'imported'""",
                      (new_desc, new_model))
    # 編輯完成回到列表（保留搜尋條件）
    from urllib.parse import urlencode
    suffix = ("?" + urlencode({"q": q})) if q else ""
    return RedirectResponse(f"/products{suffix}", 303)


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


@app.post("/products/{i}/stock/edit")
async def prod_stock_edit(i: int, request: Request):
    """庫存分布列的行內編輯：更新該（位置＋餘料屬性）桶內仍有剩餘量的進貨明細的位置與備註，
    並同步在庫序號的目前位置。"""
    form = await request.form()
    old_loc_id = int(form.get("location_id")) if form.get("location_id") else None
    is_surplus = 1 if form.get("is_surplus") == "1" else 0
    loc_code = (form.get("location_code") or "").strip()
    note = (form.get("note") or "").strip() or None
    with db.tx() as c:
        new_loc_id = get_or_create_location(c, loc_code)
        lines = c.execute("""SELECT id FROM inbound_lines
                             WHERE product_id=? AND is_surplus=?
                               AND (location_id IS ? OR location_id = ?)""",
                          (i, is_surplus, old_loc_id, old_loc_id)).fetchall()
        if not lines:
            raise HTTPException(409, "此庫存桶找不到進貨明細可更新")
        # 整桶搬移：進貨明細與對應的歷史出貨明細一起改位置，
        # 否則出貨扣帳留在舊位置會產生負數桶
        for r in lines:
            c.execute("UPDATE inbound_lines SET location_id=? WHERE id=?",
                      (new_loc_id, r["id"]))
            if _line_remaining(c, r["id"]) > 0:
                c.execute("UPDATE inbound_lines SET note=? WHERE id=?", (note, r["id"]))
            c.execute("""UPDATE serial_items SET current_location_id=?
                         WHERE inbound_line_id=? AND status IN ('in_stock','returned')""",
                      (new_loc_id, r["id"]))
        c.execute("""UPDATE outbound_lines SET from_location_id=?
                     WHERE product_id=? AND from_surplus=?
                       AND (from_location_id IS ? OR from_location_id = ?)""",
                  (new_loc_id, i, is_surplus, old_loc_id, old_loc_id))
    return RedirectResponse(f"/products/{i}", 303)


@app.get("/products/{i}", response_class=HTMLResponse)
def prod_detail(request: Request, i: int):
    p = fetch_one("""SELECT p.*, b.name brand FROM products p LEFT JOIN brands b ON b.id=p.brand_id WHERE p.id=?""", (i,))
    if not p:
        raise HTTPException(404)
    stock = [dict(r) for r in fetch_all("""
      SELECT sb.location_id, l.code loc, sb.is_surplus, sb.qty
      FROM stock_balance sb LEFT JOIN locations l ON l.id=sb.location_id
      WHERE sb.product_id=? AND sb.qty<>0
    """, (i,))]
    # 庫存分布備註/類型：彙整仍有剩餘量的進貨明細（依 位置+餘料屬性 分組）
    # 類型：正常庫存依來源進貨單類型細分 hsinchu=新竹採購 / office=台南採購
    note_map = {}
    type_map = {}
    with db.tx() as c:
        for r in c.execute("""SELECT il.id, il.location_id, il.is_surplus, il.note, io.type io_type
                              FROM inbound_lines il
                              JOIN inbound_orders io ON io.id = il.inbound_id
                              WHERE il.product_id=?""", (i,)).fetchall():
            if _line_remaining(c, r["id"]) <= 0:
                continue
            key = (r["location_id"], r["is_surplus"])
            if r["note"] and r["note"].strip():
                notes = note_map.setdefault(key, [])
                if r["note"] not in notes:
                    notes.append(r["note"])
            if not r["is_surplus"]:
                label = "新竹採購" if r["io_type"] == "hsinchu" else "台南採購"
                types = type_map.setdefault(key, [])
                if label not in types:
                    types.append(label)
    for s in stock:
        key = (s["location_id"], s["is_surplus"])
        s["notes"] = "\n".join(note_map.get(key, []))
        s["src_types"] = type_map.get(key) or ([] if s["is_surplus"] else ["台南採購"])
    # === 進出貨歷史：以「件」為單位，每列同時呈現進/出狀態 ===
    # 一、有序號的 serial_items → 每筆 1 列
    serial_history = fetch_all("""
      SELECT si.id, si.serial_no, si.status,
             il.inbound_id, io.date inbound_date, io.type inbound_type,
             il.is_surplus, il.page_no,
             ipj.job_no inbound_job, ipj.owner inbound_owner,
             ipj.project_name inbound_project_name,
             si.outbound_line_id, oo.id outbound_id, oo.date outbound_date,
             oo.type outbound_type, oo.op_kind out_op_kind,
             opj.job_no outbound_job, opj.owner outbound_owner,
             opj.project_name outbound_project_name,
             ipj.id inbound_pj_id, opj.id outbound_pj_id
      FROM serial_items si
      LEFT JOIN inbound_lines il ON il.id = si.inbound_line_id
      LEFT JOIN inbound_orders io ON io.id = il.inbound_id
      LEFT JOIN projects ipj ON ipj.id = COALESCE(il.project_id, io.project_id)
      LEFT JOIN outbound_lines ol ON ol.id = si.outbound_line_id
      LEFT JOIN outbound_orders oo ON oo.id = ol.outbound_id
      LEFT JOIN projects opj ON opj.id = oo.project_id
      WHERE si.product_id = ?
    """, (i,))
    history = []
    for r in serial_history:
        d = dict(r)
        # 工號 / 業主 / 案名：有出貨 → outbound 優先；否則 inbound
        d["job_no"] = d["outbound_job"] or d["inbound_job"]
        d["job_pid"] = d["outbound_pj_id"] if d["outbound_job"] else d["inbound_pj_id"]
        d["owner"] = d["outbound_owner"] or d["inbound_owner"]
        d["project_name"] = d["outbound_project_name"] or d["inbound_project_name"]
        # 狀態判定
        if d["outbound_id"]:
            d["state"] = "borrow" if d["out_op_kind"] == "borrow" else "shipped"
        elif d["status"] == "returned":
            d["state"] = "returned"
        else:
            d["state"] = "in_stock"
        history.append(d)

    # 二+三、無序號區塊：用 FIFO 配對每筆出貨與其來源進貨，保留進貨資訊
    nonser_rows = fetch_all("""
      SELECT il.id line_id, il.qty line_qty, il.is_surplus, il.page_no,
             io.id inbound_id, io.date inbound_date, io.type inbound_type,
             ipj.id inbound_pj_id, ipj.job_no inbound_job, ipj.owner inbound_owner,
             ipj.project_name inbound_project_name,
             (SELECT COUNT(*) FROM serial_items si
              WHERE si.inbound_line_id = il.id) sn_count
      FROM inbound_lines il
      JOIN inbound_orders io ON io.id = il.inbound_id
      LEFT JOIN projects ipj ON ipj.id = COALESCE(il.project_id, io.project_id)
      WHERE il.product_id = ?
    """, (i,))
    in_chunks = []
    for r in sorted(nonser_rows, key=lambda x: (x["inbound_date"] or "", x["inbound_id"] or 0)):
        line_non = float(r["line_qty"]) - float(r["sn_count"] or 0)
        if line_non > 0:
            in_chunks.append({
                "qty_left": line_non,
                "line_id": r["line_id"],
                "inbound_id": r["inbound_id"], "inbound_date": r["inbound_date"],
                "inbound_type": r["inbound_type"], "is_surplus": r["is_surplus"],
                "inbound_job": r["inbound_job"],
                "inbound_pj_id": r["inbound_pj_id"],
                "inbound_owner": r["inbound_owner"],
                "inbound_project_name": r["inbound_project_name"],
                "page_no": r["page_no"],
            })

    nonser_outs = fetch_all("""
      SELECT ol.id, ol.qty, ol.from_surplus is_surplus,
             ol.source_inbound_line_id,
             oo.id outbound_id, oo.date outbound_date, oo.type outbound_type,
             oo.op_kind out_op_kind,
             opj.id outbound_pj_id, opj.job_no outbound_job, opj.owner outbound_owner,
             opj.project_name outbound_project_name
      FROM outbound_lines ol
      JOIN outbound_orders oo ON oo.id = ol.outbound_id
      LEFT JOIN projects opj ON opj.id = oo.project_id
      WHERE ol.product_id = ?
        AND NOT EXISTS (SELECT 1 FROM serial_items si WHERE si.outbound_line_id = ol.id)
      ORDER BY oo.date ASC, oo.id ASC
    """, (i,))
    def _next_src(chunks, ob_date, src_il_id=None):
        # 1) 明確 source；2) FIFO
        if src_il_id:
            exp = next((c for c in chunks if c["qty_left"] > 0
                          and c.get("line_id") == src_il_id), None)
            if exp:
                return exp
        fifo = sorted([c for c in chunks if c["qty_left"] > 0],
                       key=lambda x: (x["inbound_date"] or "", x["inbound_id"] or 0))
        return fifo[0] if fifo else None
    for ob in nonser_outs:
        ob = dict(ob)
        remaining = float(ob["qty"])
        while remaining > 0:
            src = _next_src(in_chunks, ob["outbound_date"], ob.get("source_inbound_line_id"))
            if not src:
                history.append({
                    "serial_no": None, "non_serial_qty": int(remaining),
                    "inbound_id": None, "inbound_date": None, "inbound_type": None,
                    "is_surplus": ob["is_surplus"], "inbound_job": None,
                    "page_no": None,
                    "outbound_id": ob["outbound_id"], "outbound_date": ob["outbound_date"],
                    "outbound_type": ob["outbound_type"], "out_op_kind": ob["out_op_kind"],
                    "outbound_job": ob["outbound_job"],
                    "job_no": ob["outbound_job"],
                    "job_pid": ob["outbound_pj_id"],
                    "owner": ob["outbound_owner"],
                    "project_name": ob["outbound_project_name"],
                    "state": ("borrow" if ob["out_op_kind"] == "borrow" else "shipped"),
                })
                remaining = 0
                break
            take = min(remaining, src["qty_left"])
            history.append({
                "serial_no": None, "non_serial_qty": int(take),
                "inbound_id": src["inbound_id"], "inbound_date": src["inbound_date"],
                "inbound_type": src["inbound_type"], "is_surplus": src["is_surplus"],
                "inbound_job": src["inbound_job"],
                "page_no": src["page_no"],
                "outbound_id": ob["outbound_id"], "outbound_date": ob["outbound_date"],
                "outbound_type": ob["outbound_type"], "out_op_kind": ob["out_op_kind"],
                "outbound_job": ob["outbound_job"],
                "job_no": ob["outbound_job"] or src["inbound_job"],
                "job_pid": ob["outbound_pj_id"] if ob["outbound_job"] else src["inbound_pj_id"],
                "owner": ob["outbound_owner"] or src["inbound_owner"],
                "project_name": ob["outbound_project_name"] or src["inbound_project_name"],
                "state": ("borrow" if ob["out_op_kind"] == "borrow" else "shipped"),
            })
            src["qty_left"] -= take
            remaining -= take
    # 剩餘的 in_chunks 即在庫的非序號量
    for c in in_chunks:
        if c["qty_left"] <= 0:
            continue
        history.append({
            "serial_no": None, "non_serial_qty": int(c["qty_left"]),
            "inbound_id": c["inbound_id"], "inbound_date": c["inbound_date"],
            "inbound_type": c["inbound_type"], "is_surplus": c["is_surplus"],
            "inbound_job": c["inbound_job"],
            "page_no": c["page_no"],
            "outbound_id": None, "outbound_date": None,
            "outbound_type": None, "out_op_kind": None, "outbound_job": None,
            "job_no": c["inbound_job"],
            "job_pid": c["inbound_pj_id"],
            "owner": c["inbound_owner"],
            "project_name": c["inbound_project_name"],
            "state": "in_stock_non",
        })

    # 排序：進貨日新→舊（無進貨日者以出貨日代替）；同進貨日再以出貨日新→舊
    def _key(x):
        in_d = x["inbound_date"] or x["outbound_date"] or ""
        out_d = x["outbound_date"] or ""
        return (in_d, x["inbound_id"] or 0, out_d)
    history.sort(key=_key, reverse=True)

    # 彙總：以（進貨單, 出貨單）配對為單位合併，序號收進清單供前端展開
    hist_groups = []
    grp_map = {}
    for r in history:
        gk = (r.get("inbound_id"), r.get("outbound_id"), r["state"], int(r.get("is_surplus") or 0))
        g = grp_map.get(gk)
        if g is None:
            g = dict(r)
            g["qty"] = 0
            g["serials"] = []
            grp_map[gk] = g
            hist_groups.append(g)
        if r.get("serial_no"):
            g["qty"] += 1
            g["serials"].append({"id": r.get("id"), "serial_no": r["serial_no"]})
        else:
            g["qty"] += int(r.get("non_serial_qty") or 0)
    total_units = sum(g["qty"] for g in hist_groups)
    return render(request, "product_detail.html", p=p, stock=stock,
                  history=hist_groups, total_units=total_units)


# ---------- 進貨 ----------
@app.get("/inbound", response_class=HTMLResponse)
def in_list(request: Request, pending: int = 0, job_no: str = ""):
    clauses, params = [], []
    if pending:
        clauses.append("io.photo_sent=0")
    if job_no:
        clauses.append("""(
          p.job_no=? OR
          EXISTS (SELECT 1 FROM inbound_lines il2
                  JOIN projects pj2 ON pj2.id=il2.project_id
                  WHERE il2.inbound_id=io.id AND pj2.job_no=?)
        )""")
        params.extend([job_no, job_no])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = fetch_all(f"""
      SELECT io.*,
             COALESCE(io.extra_suppliers, s.name) supplier,
             st.name signer,
             COALESCE(
               (SELECT GROUP_CONCAT(DISTINCT pj.job_no) FROM inbound_lines il
                JOIN projects pj ON pj.id=il.project_id
                WHERE il.inbound_id=io.id AND pj.job_no IS NOT NULL),
               io.extra_job_nos, p.job_no
             ) job_no,
             COALESCE(
               (SELECT GROUP_CONCAT(DISTINCT il.po_no) FROM inbound_lines il
                WHERE il.inbound_id=io.id AND il.po_no IS NOT NULL AND il.po_no<>''),
               po.po_no, io.po_no
             ) po_nos,
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
      SELECT DISTINCT pj.job_no FROM inbound_lines il
      JOIN projects pj ON pj.id=il.project_id
      WHERE pj.job_no IS NOT NULL AND pj.job_no<>''
      UNION
      SELECT DISTINCT p.job_no FROM inbound_orders io
      JOIN projects p ON p.id=io.project_id
      WHERE p.job_no IS NOT NULL AND p.job_no<>''
      ORDER BY job_no DESC
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


@app.post("/inbound/{i}/photo_reset")
def in_photo_reset(request: Request, i: int):
    with db.tx() as c:
        c.execute("UPDATE inbound_orders SET photo_sent=0, photo_sent_date=NULL WHERE id=?", (i,))
    return RedirectResponse(f"/inbound/{i}", 303)


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
        "suppliers": rows_to_dicts(fetch_all("SELECT * FROM suppliers ORDER BY name")),
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
        project_id = int(form.get("project_id")) if form.get("project_id") else None
        # 單頭 supplier_id 先留空，等明細插完後依 line 彙整補上
        cur = c.execute("""INSERT INTO inbound_orders(type, date, supplier_id, signer_id, po_id, project_id, note)
                           VALUES(?,?,?,?,?,?,?)""",
                        (t, form.get("date"), None,
                         int(form.get("signer_id")) if form.get("signer_id") else None,
                         po_id,
                         project_id,
                         form.get("note", "")))
        in_id = cur.lastrowid

        # 解析多筆 line
        product_ids = form.getlist("line_product_id")
        qtys = form.getlist("line_qty")
        units = form.getlist("line_unit")
        loc_codes = form.getlist("line_location_code")
        sources = form.getlist("line_source_outbound_line_id")
        serials_json = form.getlist("line_serials")
        line_supplier_ids = form.getlist("line_supplier_id")
        is_surplus_flag = 1 if t == "surplus_return" else 0
        # 用於彙整成單頭的 supplier_id / extra_suppliers（顯示相容）
        used_supplier_ids = []  # ordered, distinct

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
            # 餘料一律進自由池（憲法：surplus → project_id NULL）
            line_project_id = None if is_surplus_flag else project_id
            # 每行供應商
            line_supplier_id = None
            if idx < len(line_supplier_ids) and line_supplier_ids[idx]:
                try:
                    line_supplier_id = int(line_supplier_ids[idx])
                    if line_supplier_id not in used_supplier_ids:
                        used_supplier_ids.append(line_supplier_id)
                except ValueError:
                    line_supplier_id = None
            cur2 = c.execute("""INSERT INTO inbound_lines
                (inbound_id, product_id, qty, unit, location_id, is_surplus, source_outbound_line_id, project_id, supplier_id, po_no)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (in_id, int(pid), qty, units[idx] if idx < len(units) else None,
                 loc_id, is_surplus_flag, src, line_project_id, line_supplier_id,
                 (form.get("po_no") or "").strip() or None))
            line_id = cur2.lastrowid
            # 序號處理
            sns_raw = serials_json[idx] if idx < len(serials_json) else ""
            sns_in = [s.strip() for s in sns_raw.replace(",", "\n").splitlines() if s.strip()]
            sns = [db.normalize_sn(s) for s in sns_in]
            sns = [s for s in sns if s]
            for sn in sns:
                if t == "surplus_return":
                    # 餘料回入庫一律歸自由池（project_id=NULL）
                    existing = c.execute("""SELECT id FROM serial_items
                                            WHERE product_id=? AND serial_no=?
                                              AND status IN ('in_stock','returned')""",
                                          (int(pid), sn)).fetchone()
                    if existing:
                        c.execute("""UPDATE serial_items SET status='returned', is_surplus=1,
                                     current_location_id=?, inbound_line_id=?, project_id=NULL
                                     WHERE id=?""", (loc_id, line_id, existing["id"]))
                    else:
                        c.execute("""INSERT INTO serial_items(product_id, serial_no, status, current_location_id,
                                     inbound_line_id, is_surplus, project_id) VALUES(?,?,?,?,?,1,NULL)""",
                                  (int(pid), sn, "returned", loc_id, line_id))
                else:
                    c.execute("""INSERT OR IGNORE INTO serial_items(product_id, serial_no, status,
                                 current_location_id, inbound_line_id, is_surplus, project_id)
                                 VALUES(?,?,?,?,?,?,?)""",
                              (int(pid), sn, "in_stock", loc_id, line_id, 0, project_id))
        # 依各 line 彙整 supplier 回填單頭（顯示相容）
        if used_supplier_ids:
            primary_sup = used_supplier_ids[0]
            extra_sup_text = None
            if len(used_supplier_ids) > 1:
                names = c.execute(
                    "SELECT id, name FROM suppliers WHERE id IN (%s)" %
                    ",".join("?" * len(used_supplier_ids)),
                    tuple(used_supplier_ids),
                ).fetchall()
                name_map = {r["id"]: r["name"] for r in names}
                extra_sup_text = "\n".join(name_map.get(i, "") for i in used_supplier_ids if name_map.get(i))
            c.execute("UPDATE inbound_orders SET supplier_id=?, extra_suppliers=? WHERE id=?",
                      (primary_sup, extra_sup_text, in_id))
    return RedirectResponse(f"/inbound/{in_id}", 303)


@app.get("/inbound/{i}", response_class=HTMLResponse)
def in_detail(request: Request, i: int):
    head = fetch_one("""
      SELECT io.id, io.type, io.date, io.supplier_id, io.signer_id, io.po_id,
             io.project_id, io.loan_id, io.note, io.photo_sent, io.photo_sent_date,
             io.created_at, io.extra_job_nos, io.extra_suppliers, io.is_borrow_return,
             COALESCE(io.extra_suppliers, s.name) AS supplier,
             st.name AS signer,
             COALESCE(io.extra_job_nos, p.job_no) AS job_no,
             COALESCE(
               (SELECT GROUP_CONCAT(DISTINCT il.po_no) FROM inbound_lines il
                WHERE il.inbound_id=io.id AND il.po_no IS NOT NULL AND il.po_no<>''),
               po.po_no, io.po_no
             ) AS po_no,
             rq.name AS requester
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
      SELECT il.*, b.name brand, p.model, p.description, l.code loc,
             sp.name supplier,
             COALESCE(pj.job_no, hpj.job_no) job_no,
             COALESCE(pj.owner, hpj.owner) job_owner,
             COALESCE(il.project_id, io.project_id) eff_project_id
      FROM inbound_lines il
      JOIN inbound_orders io ON io.id=il.inbound_id
      JOIN products p ON p.id=il.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=il.location_id
      LEFT JOIN suppliers sp ON sp.id=il.supplier_id
      LEFT JOIN projects pj ON pj.id=il.project_id
      LEFT JOIN projects hpj ON hpj.id=io.project_id
      WHERE il.inbound_id=?
    """, (i,))
    serial_rows = fetch_all("""
      SELECT id, serial_no, inbound_line_id, status
      FROM serial_items
      WHERE inbound_line_id IN (SELECT id FROM inbound_lines WHERE inbound_id=?)
      ORDER BY serial_no
    """, (i,))
    serials_by_line = {}
    for r in serial_rows:
        serials_by_line.setdefault(r["inbound_line_id"], []).append(dict(r))

    line_avail = {}
    with db.tx() as cn:
        for l in lines:
            line_avail[l["id"]] = _line_remaining(cn, l["id"])
    # 工號改由明細層 inbound_lines.project_id 聚合 distinct 顯示
    projects_info = [dict(r) for r in fetch_all("""
      SELECT DISTINCT pj.id, pj.job_no, pj.owner, pj.project_name
      FROM inbound_lines il
      JOIN projects pj ON pj.id = il.project_id
      WHERE il.inbound_id=? AND il.project_id IS NOT NULL
      ORDER BY pj.job_no
    """, (i,))]
    all_suppliers = fetch_all("SELECT id, name FROM suppliers ORDER BY name")
    all_projects = fetch_all("SELECT id, job_no, owner FROM projects ORDER BY job_no DESC")
    return render(request, "inbound_detail.html", h=head, lines=lines,
                  serials_by_line=serials_by_line, projects_info=projects_info,
                  all_suppliers=all_suppliers, all_projects=all_projects,
                  line_avail=line_avail)


@app.post("/inbound/{i}/line/{lid}/note")
def in_line_note(i: int, lid: int, note: str = Form("")):
    with db.tx() as c:
        line = c.execute(
            "SELECT id FROM inbound_lines WHERE id=? AND inbound_id=?", (lid, i)
        ).fetchone()
        if not line:
            raise HTTPException(404, "明細不存在")
        c.execute("UPDATE inbound_lines SET note=? WHERE id=?",
                  (note.strip() or None, lid))
    return RedirectResponse(f"/inbound/{i}", 303)


@app.post("/inbound/{i}/line/{lid}/del")
def in_line_del(i: int, lid: int):
    with db.tx() as c:
        line = c.execute(
            "SELECT id FROM inbound_lines WHERE id=? AND inbound_id=?", (lid, i)
        ).fetchone()
        if not line:
            raise HTTPException(404, "明細不存在")
        # 阻擋：任一序號已有後續流向（出貨/借出）
        moved = c.execute(
            """SELECT serial_no, status FROM serial_items
               WHERE inbound_line_id=? AND status NOT IN ('in_stock','returned')""",
            (lid,),
        ).fetchall()
        if moved:
            names = ", ".join(f"{m['serial_no']}({m['status']})" for m in moved[:10])
            raise HTTPException(409, f"以下序號已有後續流向，無法刪除此明細：{names}")
        # 阻擋：此 line 被出貨單指定為來源
        used = c.execute(
            "SELECT COUNT(*) n FROM outbound_lines WHERE source_inbound_line_id=?", (lid,)
        ).fetchone()["n"]
        if used:
            raise HTTPException(409, f"此明細已被 {used} 筆出貨紀錄指定為來源，無法刪除")
        # 清乾淨仍在庫/退回狀態的序號
        c.execute("""DELETE FROM serial_items
                     WHERE inbound_line_id=? AND status IN ('in_stock','returned')""",
                  (lid,))
        # cart_items.inbound_line_id ON DELETE CASCADE 會自動清除
        c.execute("DELETE FROM inbound_lines WHERE id=?", (lid,))
    return RedirectResponse(f"/inbound/{i}", 303)


@app.post("/inbound/{i}/line/{lid}/edit")
async def in_line_edit(i: int, lid: int, request: Request):
    form = await request.form()
    try:
        qty = float(form.get("qty") or 0)
    except ValueError:
        raise HTTPException(400, "數量格式錯誤")
    if qty <= 0:
        raise HTTPException(400, "數量必須大於 0")
    unit = (form.get("unit") or "").strip() or None
    loc_code = (form.get("location_code") or "").strip()
    is_surplus = 1 if form.get("is_surplus") else 0
    supplier_id = int(form.get("supplier_id")) if form.get("supplier_id") else None
    project_id = int(form.get("project_id")) if form.get("project_id") else None
    po_no = (form.get("po_no") or "").strip() or None
    with db.tx() as c:
        line = c.execute(
            "SELECT * FROM inbound_lines WHERE id=? AND inbound_id=?", (lid, i)
        ).fetchone()
        if not line:
            raise HTTPException(404, "明細不存在")
        # 若該行已有序號被後續流向（出貨/借出等），擋下「位置 / 餘料」變更
        locked_status = ("in_stock", "returned")
        moved = c.execute(
            """SELECT serial_no, status FROM serial_items
               WHERE inbound_line_id=? AND status NOT IN ('in_stock','returned')""",
            (lid,),
        ).fetchall()
        loc_id = get_or_create_location(c, loc_code)
        # 防呆：若有序號已流向且試圖改 location/is_surplus
        if moved and (loc_id != line["location_id"] or is_surplus != line["is_surplus"]):
            names = ", ".join(f"{m['serial_no']}({m['status']})" for m in moved[:10])
            raise HTTPException(409, f"以下序號已有後續流向，無法變更位置/餘料：{names}")
        c.execute(
            """UPDATE inbound_lines SET qty=?, unit=?, location_id=?, is_surplus=?, supplier_id=?, project_id=?, po_no=?
               WHERE id=?""",
            (qty, unit, loc_id, is_surplus, supplier_id, project_id, po_no, lid),
        )
        # 同步仍在庫/退回狀態的序號位置、is_surplus、project_id
        c.execute(
            """UPDATE serial_items
               SET current_location_id=?, is_surplus=?, project_id=?
               WHERE inbound_line_id=? AND status IN ('in_stock','returned')""",
            (loc_id, is_surplus, project_id, lid),
        )
        # 重新彙整單頭 supplier_id / extra_suppliers
        sup_rows = c.execute(
            """SELECT DISTINCT il.supplier_id, sp.name
               FROM inbound_lines il LEFT JOIN suppliers sp ON sp.id=il.supplier_id
               WHERE il.inbound_id=? AND il.supplier_id IS NOT NULL
               ORDER BY il.id""",
            (i,),
        ).fetchall()
        ids = [r["supplier_id"] for r in sup_rows]
        names = [r["name"] for r in sup_rows if r["name"]]
        primary = ids[0] if ids else None
        extra = "\n".join(names) if len(names) > 1 else None
        c.execute("UPDATE inbound_orders SET supplier_id=?, extra_suppliers=? WHERE id=?",
                  (primary, extra, i))
    return RedirectResponse(f"/inbound/{i}", 303)


@app.post("/inbound/{i}/line/{lid}/serials")
async def in_line_serials(i: int, lid: int, request: Request):
    form = await request.form()
    raw = form.get("serials", "")
    new_sns = []
    seen = set()
    for s in raw.replace(",", "\n").splitlines():
        sn = db.normalize_sn(s)
        if sn and sn not in seen:
            seen.add(sn)
            new_sns.append(sn)
    with db.tx() as c:
        line = c.execute("""SELECT il.*, io.type AS in_type FROM inbound_lines il
                            JOIN inbound_orders io ON io.id=il.inbound_id
                            WHERE il.id=? AND il.inbound_id=?""", (lid, i)).fetchone()
        if not line:
            raise HTTPException(404, "明細不存在")
        pid = line["product_id"]
        loc_id = line["location_id"]
        is_surplus = line["is_surplus"]
        t = line["in_type"]
        allowed_status = ("returned",) if t == "surplus_return" else ("in_stock",)
        new_status = "returned" if t == "surplus_return" else "in_stock"
        existing = c.execute(
            "SELECT * FROM serial_items WHERE inbound_line_id=?", (lid,)
        ).fetchall()
        locked = [e for e in existing if e["status"] not in allowed_status]
        if locked:
            names = ", ".join(f"{e['serial_no']}({e['status']})" for e in locked[:10])
            raise HTTPException(409, f"以下序號已有後續流向，無法修改：{names}")
        existing_by_sn = {e["serial_no"]: e for e in existing}
        new_set = set(new_sns)
        for sn, row in existing_by_sn.items():
            if sn not in new_set:
                c.execute("DELETE FROM serial_items WHERE id=?", (row["id"],))
        for sn in new_sns:
            if sn in existing_by_sn:
                continue
            dup = c.execute(
                """SELECT id FROM serial_items
                   WHERE product_id=? AND serial_no=?
                     AND status IN ('in_stock','returned')""",
                (pid, sn),
            ).fetchone()
            if dup:
                raise HTTPException(409, f"序號 {sn} 已存在於此料件（活躍中，無法重複新增）")
            # 餘料一律進自由池；其餘沿用 inbound_lines.project_id
            sn_project_id = None if is_surplus else line["project_id"]
            c.execute(
                """INSERT INTO serial_items(product_id, serial_no, status,
                   current_location_id, inbound_line_id, is_surplus, project_id)
                   VALUES(?,?,?,?,?,?,?)""",
                (pid, sn, new_status, loc_id, lid, is_surplus, sn_project_id),
            )
    return RedirectResponse(f"/inbound/{i}", 303)


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
    requester_id = int(form.get("requester_id")) if form.get("requester_id") else None
    note = form.get("note", "")
    with db.tx() as c:
        head = c.execute("SELECT id FROM inbound_orders WHERE id=?", (i,)).fetchone()
        if not head:
            raise HTTPException(404)
        # PO 改在明細層逐筆設定（inbound_lines.po_no），單頭不再編輯 PO
        if requester_id:
            c.execute("""UPDATE purchase_orders SET requester_id=?
                         WHERE id=(SELECT po_id FROM inbound_orders WHERE id=?)""",
                      (requester_id, i))
        c.execute("""UPDATE inbound_orders
                     SET date=?, supplier_id=?, signer_id=?, note=?
                     WHERE id=?""",
                  (date_v, supplier_id, signer_id, note or None, i))
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


# ---------- 序號選擇器：共用 context builder ----------
def _build_picker_ctx(mode: str, from_project_id: int, product_id: int,
                       free_pool: int, is_surplus: int = 0):
    """組裝 _serial_picker.html 所需 context（projects / products / slots / serials_by_slot...）。"""
    if mode not in ("project", "product"):
        mode = "project"
    products = rows_to_dicts(fetch_all("""
      SELECT p.id, p.model, p.base_unit, b.name brand
      FROM products p LEFT JOIN brands b ON b.id=p.brand_id
      WHERE p.is_kit=0 ORDER BY b.name, p.model
    """))
    projects = rows_to_dicts(fetch_all(
        "SELECT id, job_no, owner FROM projects ORDER BY job_no DESC"))

    slots = []
    # 預先載入「自由池內、來源為某工號餘料退回」的對照表：serial_id -> src_project_id, src_job_no
    surplus_src_map = {}
    if mode in ("project", "product"):
        for r in fetch_all("""
          SELECT si.id, io.project_id src_pid, pj.job_no src_job_no
          FROM serial_items si
          JOIN inbound_lines il ON il.id = si.inbound_line_id
          JOIN inbound_orders io ON io.id = il.inbound_id
          LEFT JOIN projects pj ON pj.id = io.project_id
          WHERE si.project_id IS NULL
            AND io.type = 'surplus_return'
            AND io.is_borrow_return = 0
            AND io.project_id IS NOT NULL
        """):
            surplus_src_map[r["id"]] = {"src_project_id": r["src_pid"], "src_job_no": r["src_job_no"]}

    if mode == "project" and (from_project_id or free_pool):
        sql = """
          SELECT sb.product_id, sb.project_id, sb.is_surplus, SUM(sb.qty) qty,
                 GROUP_CONCAT(DISTINCT l.code) locs,
                 b.name brand, p.model, p.description,
                 pj.job_no
          FROM stock_balance sb
          JOIN products p ON p.id = sb.product_id
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN locations l ON l.id = sb.location_id
          LEFT JOIN projects pj ON pj.id = sb.project_id
          WHERE sb.qty > 0 AND sb.is_surplus = ?
        """
        params = [is_surplus]
        if free_pool:
            sql += " AND sb.project_id IS NULL"
        else:
            sql += " AND sb.project_id = ?"
            params.append(from_project_id)
        if product_id:
            sql += " AND sb.product_id = ?"
            params.append(product_id)
        sql += """ GROUP BY sb.product_id, sb.project_id, sb.is_surplus
                   HAVING SUM(sb.qty) > 0
                   ORDER BY b.name, p.model """
        slots = rows_to_dicts(fetch_all(sql, params))
        for s in slots:
            s["source_kind"] = "own"
            s["src_project_id"] = None
            s["src_job_no"] = None

        # 方案 A：當選定來源工號（非自由池），追加「源自此工號餘料退回的自由池庫存」slots
        # 以 inbound_lines 為來源（含無序號數量），qty 上限取 min(該 line 帳上量, 同 product+location 自由池現存量)
        if from_project_id and not free_pool:
            src_job_no_row = fetch_one("SELECT job_no FROM projects WHERE id=?", (from_project_id,))
            src_job_no = src_job_no_row["job_no"] if src_job_no_row else None
            extra_sql = """
              SELECT il.product_id, il.location_id, il.is_surplus,
                     SUM(il.qty) line_qty,
                     l.code loc,
                     b.name brand, p.model, p.description
              FROM inbound_lines il
              JOIN inbound_orders io ON io.id = il.inbound_id
              JOIN products p ON p.id = il.product_id
              LEFT JOIN brands b ON b.id = p.brand_id
              LEFT JOIN locations l ON l.id = il.location_id
              WHERE il.project_id IS NULL AND il.is_surplus = ?
                AND io.type = 'surplus_return' AND io.is_borrow_return = 0
                AND io.project_id = ?
            """
            extra_params = [is_surplus, from_project_id]
            if product_id:
                extra_sql += " AND il.product_id = ?"
                extra_params.append(product_id)
            extra_sql += """ GROUP BY il.product_id, il.location_id, il.is_surplus
                             HAVING SUM(il.qty) > 0 """
            for r in fetch_all(extra_sql, extra_params):
                pid = r["product_id"]; lid = r["location_id"]; isurp = r["is_surplus"]
                # 取得自由池實際可用量（同 product/location）
                avail_row = fetch_one("""SELECT COALESCE(SUM(qty),0) q FROM stock_balance
                                          WHERE product_id=? AND project_id IS NULL
                                            AND is_surplus=?
                                            AND (location_id IS ? OR location_id = ?)""",
                                       (pid, isurp, lid, lid))
                avail = max(0, int(avail_row["q"] or 0))
                src_qty = min(int(r["line_qty"] or 0), avail)
                if src_qty <= 0:
                    continue
                slots.append({
                    "product_id": pid, "project_id": None, "is_surplus": isurp,
                    "qty": src_qty, "locs": r["loc"] or "(未指定)",
                    "location_id": lid,
                    "brand": r["brand"], "model": r["model"], "description": r["description"],
                    "job_no": None,
                    "source_kind": "surplus_from",
                    "src_project_id": from_project_id,
                    "src_job_no": src_job_no,
                })
    elif mode == "product" and product_id:
        sql = """
          SELECT sb.product_id, sb.project_id, sb.is_surplus, SUM(sb.qty) qty,
                 GROUP_CONCAT(DISTINCT l.code) locs,
                 b.name brand, p.model, p.description,
                 pj.job_no, pj.owner
          FROM stock_balance sb
          JOIN products p ON p.id = sb.product_id
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN locations l ON l.id = sb.location_id
          LEFT JOIN projects pj ON pj.id = sb.project_id
          WHERE sb.qty > 0 AND sb.product_id = ? AND sb.is_surplus = ?
        """
        params = [product_id, is_surplus]
        # 次級篩選：限定來源工號 / 自由池
        if free_pool:
            sql += " AND sb.project_id IS NULL"
        elif from_project_id:
            sql += " AND sb.project_id = ?"
            params.append(from_project_id)
        sql += """ GROUP BY sb.product_id, sb.project_id, sb.is_surplus
                   HAVING SUM(sb.qty) > 0
                   ORDER BY (sb.project_id IS NULL) DESC, pj.job_no """
        slots = rows_to_dicts(fetch_all(sql, params))
        for s in slots:
            s["source_kind"] = "own"
            s["src_project_id"] = None
            s["src_job_no"] = None
        # 若指定了「來源工號」，附加「源自該工號餘料退回的自由池」slots（限定本料件）
        if product_id and from_project_id and not free_pool:
            src_job_no_row = fetch_one("SELECT job_no FROM projects WHERE id=?", (from_project_id,))
            src_job_no = src_job_no_row["job_no"] if src_job_no_row else None
            for r in fetch_all("""
              SELECT il.product_id, il.location_id, il.is_surplus,
                     SUM(il.qty) line_qty, l.code loc,
                     b.name brand, p.model, p.description
              FROM inbound_lines il
              JOIN inbound_orders io ON io.id = il.inbound_id
              JOIN products p ON p.id = il.product_id
              LEFT JOIN brands b ON b.id = p.brand_id
              LEFT JOIN locations l ON l.id = il.location_id
              WHERE il.project_id IS NULL AND il.is_surplus = ?
                AND io.type = 'surplus_return' AND io.is_borrow_return = 0
                AND io.project_id = ? AND il.product_id = ?
              GROUP BY il.product_id, il.location_id, il.is_surplus
              HAVING SUM(il.qty) > 0
            """, (is_surplus, from_project_id, product_id)):
                pid = r["product_id"]; lid = r["location_id"]; isurp = r["is_surplus"]
                avail_row = fetch_one("""SELECT COALESCE(SUM(qty),0) q FROM stock_balance
                                          WHERE product_id=? AND project_id IS NULL AND is_surplus=?
                                            AND (location_id IS ? OR location_id = ?)""",
                                       (pid, isurp, lid, lid))
                src_qty = min(int(r["line_qty"] or 0), max(0, int(avail_row["q"] or 0)))
                if src_qty <= 0:
                    continue
                slots.append({
                    "product_id": pid, "project_id": None, "is_surplus": isurp,
                    "qty": src_qty, "locs": r["loc"] or "(未指定)",
                    "location_id": lid,
                    "brand": r["brand"], "model": r["model"], "description": r["description"],
                    "job_no": None, "owner": None,
                    "source_kind": "surplus_from",
                    "src_project_id": from_project_id, "src_job_no": src_job_no,
                })

    serials_by_slot = {}
    if slots:
        pid_set = {s["product_id"] for s in slots}
        placeholders = ",".join("?" * len(pid_set))
        rows = fetch_all(f"""
          SELECT si.id, si.serial_no, si.product_id, si.project_id, si.is_surplus,
                 si.current_location_id, l.code loc,
                 io.date inbound_date, io.id inbound_id, io.type inbound_type,
                 pj.job_no
          FROM serial_items si
          LEFT JOIN locations l ON l.id = si.current_location_id
          LEFT JOIN inbound_lines il ON il.id = si.inbound_line_id
          LEFT JOIN inbound_orders io ON io.id = il.inbound_id
          LEFT JOIN projects pj ON pj.id = si.project_id
          WHERE si.status IN ('in_stock', 'returned')
            AND si.is_surplus = ?
            AND si.product_id IN ({placeholders})
          ORDER BY si.serial_no
        """, (is_surplus, *pid_set))
        # 將每筆序號歸到對應的 slot：(pid, project_id, is_surplus, source_kind)
        for r in rows:
            sr = dict(r)
            src = surplus_src_map.get(sr["id"])
            if src:
                sr["src_project_id"] = src["src_project_id"]
                sr["src_job_no"] = src["src_job_no"]
            else:
                sr["src_project_id"] = None
                sr["src_job_no"] = None
            if sr["project_id"] is None and src and mode == "project" \
                    and from_project_id and src["src_project_id"] == from_project_id:
                # 源自所選工號的自由池序號 → 歸入 surplus_from slot
                key = (sr["product_id"], None, sr["is_surplus"], "surplus_from")
            else:
                key = (sr["product_id"], sr["project_id"], sr["is_surplus"], "own")
            serials_by_slot.setdefault(key, []).append(sr)
    for s in slots:
        key = (s["product_id"], s["project_id"], s["is_surplus"], s["source_kind"])
        s["serial_count"] = len(serials_by_slot.get(key, []))
        s["non_serial_qty"] = max(0, int(s["qty"]) - s["serial_count"])
    if mode == "project":
        # 排序：own 在前、surplus_from 在後；同類別中有序號者在前
        slots.sort(key=lambda s: (
            0 if s["source_kind"] == "own" else 1,
            0 if s["serial_count"] > 0 else 1,
            (s["brand"] or ""), s["model"]))

    src_project = None
    if mode == "project" and from_project_id:
        src_project = fetch_one("SELECT id, job_no, owner, project_name FROM projects WHERE id=?",
                                 (from_project_id,))
    selected_product = None
    if mode == "product" and product_id:
        selected_product = fetch_one("""SELECT p.id, p.model, p.description, b.name brand
                                        FROM products p LEFT JOIN brands b ON b.id=p.brand_id
                                        WHERE p.id=?""", (product_id,))
    return {
        "mode": mode,
        "products": products,
        "projects": projects,
        "from_project_id": from_project_id,
        "product_id": product_id,
        "free_pool": free_pool,
        "slots": slots,
        "serials_by_slot": serials_by_slot,
        "src_project": src_project,
        "selected_product": selected_product,
    }


def _consume_serials_for_outbound(c, serial_ids: list,
                                   to_project_id: int, is_surplus: int,
                                   date_v: str,
                                   notifier_id=None, recipient="", signer_id=None,
                                   sign_date=None, shipping_carrier="", shipping_no="",
                                   note="", nonser_picks: list = None,
                                   serial_notes: dict = None):
    """nonser_picks: [{pid, loc, src, qty}] — 從來自某工號餘料退回、無序號的自由池項目扣帳。
    serial_notes: {serial_item_id: 使用者備註} — 併入對應出貨明細的 note。"""
    nonser_picks = nonser_picks or []
    serial_notes = serial_notes or {}
    if not serial_ids and not nonser_picks:
        raise HTTPException(400, "請至少勾選一筆序號或填入無序號取用數量")
    sn_rows = []
    if serial_ids:
        placeholders = ",".join("?" * len(serial_ids))
        sn_rows = c.execute(f"""
          SELECT si.id, si.product_id, si.project_id, si.is_surplus, si.status,
                 si.current_location_id
          FROM serial_items si
          WHERE si.id IN ({placeholders})
        """, tuple(serial_ids)).fetchall()
        if len(sn_rows) != len(serial_ids):
            raise HTTPException(400, "部分勾選序號不存在")
        for r in sn_rows:
            if r["status"] not in ("in_stock", "returned"):
                raise HTTPException(409, f"序號 #{r['id']} 狀態 {r['status']} 不可出貨")
            if r["is_surplus"] != is_surplus:
                raise HTTPException(409, f"序號 #{r['id']} 餘料屬性與表單不一致")

    cur = c.execute("""INSERT INTO outbound_orders(type, date, notifier_id, recipient, signer_id,
                       sign_date, project_id, shipping_carrier, shipping_no, note, op_kind)
                       VALUES(?,?,?,?,?,?,?,?,?,?,'ship')""",
                    ("normal" if not is_surplus else "surplus_transfer",
                     date_v, notifier_id, recipient or "", signer_id,
                     sign_date or None, to_project_id,
                     shipping_carrier or "", shipping_no or "", note or ""))
    out_id = cur.lastrowid

    # 依（product_id, from_project_id, location）分組寫 outbound_lines
    groups = {}
    sn_meta = {r["id"]: dict(r) for r in sn_rows}
    for sid in serial_ids:
        m = sn_meta[sid]
        key = (m["product_id"], m["project_id"], m["current_location_id"])
        groups.setdefault(key, []).append(sid)

    for (pid, from_pid, loc_id), sids in groups.items():
        qty = float(len(sids))
        note_parts = []
        if from_pid and from_pid != to_project_id:
            pj = c.execute("SELECT job_no FROM projects WHERE id=?", (from_pid,)).fetchone()
            note_parts.append(f"借自 {pj['job_no']}" if pj else f"借自 #{from_pid}")
        elif from_pid is None:
            # 自由池：若整組序號均源自同一個工號的餘料退回，標註來源
            src_rows = c.execute(f"""
              SELECT DISTINCT io.project_id, pj.job_no
              FROM serial_items si
              JOIN inbound_lines il ON il.id = si.inbound_line_id
              JOIN inbound_orders io ON io.id = il.inbound_id
              LEFT JOIN projects pj ON pj.id = io.project_id
              WHERE si.id IN ({','.join(['?']*len(sids))})
                AND io.type = 'surplus_return' AND io.is_borrow_return = 0
                AND io.project_id IS NOT NULL
            """, tuple(sids)).fetchall()
            if src_rows:
                labels = [r["job_no"] or f"#{r['project_id']}" for r in src_rows]
                note_parts.append("源自 " + "/".join(labels) + " 餘料")
        user_notes = []
        for sid in sids:
            un = serial_notes.get(sid)
            if un and un not in user_notes:
                user_notes.append(un)
        note_parts.extend(user_notes)
        line_note = " / ".join(note_parts) or None

        cur2 = c.execute("""INSERT INTO outbound_lines(outbound_id, product_id, qty, unit,
                            from_location_id, from_surplus, note, from_project_id)
                            VALUES(?,?,?,?,?,?,?,?)""",
                         (out_id, pid, qty, None, loc_id, is_surplus, line_note, from_pid))
        line_id = cur2.lastrowid
        for sid in sids:
            c.execute("""UPDATE serial_items SET status='shipped',
                         outbound_line_id=?, current_location_id=NULL
                         WHERE id=?""", (line_id, sid))

    # 處理無序號取用（可從自由池、自有工號池、或來自他工號之餘料退回）
    for np in nonser_picks:
        pid = int(np["pid"]); loc_id = int(np["loc"]) or None
        from_pid = int(np.get("frompid") or 0) or None  # stock_balance.project_id；0/None 為自由池
        src_pid = int(np.get("src") or 0) or None  # 餘料來源工號（若為自由池且源於某工號餘料退回）
        qty = float(np["qty"])
        if qty <= 0:
            continue
        # 驗證該池實際可用量
        if from_pid is None:
            avail_row = c.execute("""SELECT COALESCE(SUM(qty),0) q FROM stock_balance
                                      WHERE product_id=? AND project_id IS NULL AND is_surplus=?
                                        AND (location_id IS ? OR location_id = ?)""",
                                  (pid, is_surplus, loc_id, loc_id)).fetchone()
        else:
            avail_row = c.execute("""SELECT COALESCE(SUM(qty),0) q FROM stock_balance
                                      WHERE product_id=? AND project_id=? AND is_surplus=?
                                        AND (location_id IS ? OR location_id = ?)""",
                                  (pid, from_pid, is_surplus, loc_id, loc_id)).fetchone()
        avail = avail_row["q"] or 0
        if qty > avail:
            prow = c.execute("SELECT model FROM products WHERE id=?", (pid,)).fetchone()
            pool_label = "自由池" if from_pid is None else f"工號池 #{from_pid}"
            raise HTTPException(400, f"[{prow['model'] if prow else pid}] {pool_label}可用 {avail}，不足 {int(qty)}")

        line_note_parts = []
        if from_pid and from_pid != to_project_id:
            pj = c.execute("SELECT job_no FROM projects WHERE id=?", (from_pid,)).fetchone()
            line_note_parts.append(f"借自 {pj['job_no']}" if pj else f"借自 #{from_pid}")
        if src_pid:
            src_proj_row = c.execute("SELECT job_no FROM projects WHERE id=?", (src_pid,)).fetchone()
            src_label = src_proj_row["job_no"] if src_proj_row else f"#{src_pid}"
            line_note_parts.append(f"源自 {src_label} 餘料")
        if np.get("note"):
            line_note_parts.append(np["note"])

        src_il_id = np.get("src_inbound_line_id")
        cur_ol = c.execute("""INSERT INTO outbound_lines(outbound_id, product_id, qty, unit,
                              from_location_id, from_surplus, note, from_project_id,
                              source_inbound_line_id)
                              VALUES(?,?,?,?,?,?,?,?,?)""",
                           (out_id, pid, qty, None, loc_id, is_surplus,
                            " / ".join(line_note_parts) or None, from_pid, src_il_id))
        line_id = cur_ol.lastrowid

        # 序號為「可選」：部分料件不追蹤序號。
        # 若有填則建立 serial_items 並標記 shipped；剩餘數量僅做帳上扣除。
        provided_sns = [s.strip() for s in (np.get("serials") or []) if (s or "").strip()]
        if provided_sns:
            if len(provided_sns) > int(qty):
                raise HTTPException(400,
                    f"料件 #{pid} 取用 {int(qty)} 件但提供了 {len(provided_sns)} 筆序號（超量）")
            for raw_sn in provided_sns:
                sn = db.normalize_sn(raw_sn)
                if not sn:
                    continue
                # 防呆：禁止與活躍序號重複（已 shipped 的歷史紀錄可共存）
                dup = c.execute("""SELECT id FROM serial_items
                                    WHERE product_id=? AND serial_no=?
                                      AND status IN ('in_stock','returned')""",
                                (pid, sn)).fetchone()
                if dup:
                    raise HTTPException(409, f"序號 {sn} 已存在於活躍庫存（料件 #{pid}），請改用另一個序號")
                # 直接建立並標記 shipped
                c.execute("""INSERT INTO serial_items(product_id, serial_no, status,
                             current_location_id, outbound_line_id, is_surplus, project_id)
                             VALUES(?,?,?,?,?,?,?)""",
                          (pid, sn, "shipped", None, line_id, is_surplus, None))
    return out_id


# ---------- Raw 暫存區（excel 校正用）----------
_RAW_COLUMNS_PADDING = ["po_no", "photo_sent"]  # 確保 update 路由白名單
RAW_COLUMNS = [
    ("item_no", "ITEM"),
    ("date", "日期"),
    ("signer", "簽收人"),
    ("source", "出貨對象"),
    ("model", "商品名稱"),
    ("description", "商品敘述"),
    ("serial_no", "序號"),
    ("qty", "數量"),
    ("project_no", "工號"),
    ("owner", "案主"),
    ("project_name", "案名"),
    ("note", "備註"),
    ("stock_item", "庫存料件"),
    ("stock_qty", "庫存數量"),
    ("location", "位置"),
    ("picker", "取放人"),
    ("ledger_no", "領出/借出單號"),
    ("page_no", "進貨單頁次"),
    ("code_pos", "編號位置"),
    ("color", "color"),
    ("po_no", "PO"),
    ("photo_sent", "回傳"),
]


RAW_HEADER_COLS = [
    ("item_no", "ITEM"),
    ("date", "日期"),
    ("signer", "簽收人"),
    ("source", "出貨對象"),
    ("po_no", "PO"),
    ("picker", "取放人"),
    ("ledger_no", "領出/借出單號"),
    ("photo_sent", "回傳"),
    ("color", "color"),
]
RAW_LINE_COLS = [
    ("model", "商品名稱"),
    ("description", "商品敘述"),
    ("serial_no", "序號"),
    ("qty", "數量"),
    ("project_no", "工號"),
    ("page_no", "進貨單頁次"),
    ("location", "位置"),
    ("code_pos", "編號位置"),
]


@app.get("/raw", response_class=HTMLResponse)
def raw_list(request: Request):
    # 進貨單若已被刪除 → 自動回復對應 raw 列為 pending（允許重新匯入）
    with db.tx() as c:
        c.execute("""UPDATE raw_imports
                     SET status='pending', imported_ref_id=NULL, imported_at=NULL
                     WHERE status='imported'
                       AND (imported_ref_id IS NULL
                            OR imported_ref_id NOT IN (SELECT id FROM inbound_orders))""")
    # 新資料在上、舊資料在下：以 ITEM 編號數值倒序，再以 id 倒序
    rows = fetch_all("""SELECT * FROM raw_imports
                        ORDER BY (item_no IS NULL),
                                 CAST(item_no AS INTEGER) DESC,
                                 id DESC""")
    total = len(rows)
    pending = sum(1 for r in rows if r["status"] == "pending")
    imported = sum(1 for r in rows if r["status"] == "imported")

    # 以 ITEM 分組；同 item_no 為一組。item_no 為空者各自獨立成單行群組。
    groups = []
    by_item = {}
    for r in rows:
        d = dict(r)
        item_no = d.get("item_no")
        key = item_no if item_no else f"__solo_{d['id']}"
        if key not in by_item:
            by_item[key] = {"item_no": item_no, "lines": [],
                            "header": {k: None for k, _ in RAW_HEADER_COLS}}
            groups.append(by_item[key])
        g = by_item[key]
        g["lines"].append(d)
        # 取每欄第一個非空值作為 header
        for k, _ in RAW_HEADER_COLS:
            if g["header"].get(k) in (None, "") and d.get(k) not in (None, ""):
                g["header"][k] = d[k]
    # 計算每組匯入狀態：imported_ref_id 集合（取所有 imported 列的 ref）
    for g in groups:
        refs = {ln["imported_ref_id"] for ln in g["lines"]
                if ln["status"] == "imported" and ln["imported_ref_id"]}
        g["inbound_ref_id"] = next(iter(refs)) if len(refs) == 1 else None
        g["all_imported"] = (all(ln["status"] == "imported" for ln in g["lines"])
                             and g["inbound_ref_id"] is not None)
        g["pending_ids"] = [ln["id"] for ln in g["lines"] if ln["status"] != "imported"]

    products = fetch_all("""SELECT p.id, p.model, p.description, b.name brand
                            FROM products p LEFT JOIN brands b ON b.id=p.brand_id
                            WHERE p.is_kit=0 ORDER BY p.model""")
    suppliers = fetch_all("SELECT id, name FROM suppliers ORDER BY name")
    return render(request, "raw_list.html",
                  groups=groups, total=total,
                  pending=pending, imported=imported,
                  header_cols=RAW_HEADER_COLS, line_cols=RAW_LINE_COLS,
                  products=products, suppliers=suppliers)


@app.post("/raw/import")
async def raw_import(file: UploadFile = File(...)):
    import time
    data = await file.read()
    batch_id = f"raw_{int(time.time())}"
    stats = importer.import_raw_excel(data, batch_id)
    # 把 stats 暫存到 query string 供下個頁面展示
    return RedirectResponse(
        f"/raw?inserted={stats['rows_inserted']}&total={stats['total_rows']}"
        f"&unknown={len(stats['headers_unknown'])}", 303)


@app.post("/raw/{rid}/update")
async def raw_update(rid: int, request: Request):
    """更新單欄。Form: field=<col>, value=<new value>
    特殊：field='model' 時若值對應到 products 主檔，會自動同步 description。
    回傳 extra={'description': '...'} 以便前端同步顯示。
    """
    from fastapi.responses import JSONResponse
    form = await request.form()
    field = form.get("field")
    raw_value = form.get("value", "")
    if field not in {col for col, _ in RAW_COLUMNS}:
        raise HTTPException(400, "不允許更新此欄位")
    value = raw_value.strip() if raw_value else None
    if field in ("qty", "stock_qty") and value is not None:
        try:
            value = float(value)
        except ValueError:
            return JSONResponse({"ok": False, "msg": "數量需為數字"}, status_code=400)
        if value is not None and value != value:  # NaN guard
            value = None
    if field == "serial_no" and value:
        tokens = []
        for tok in value.replace("\n", "/").replace(",", "/").replace("，", "/").split("/"):
            n = db.normalize_sn(tok)
            if n:
                tokens.append(n)
        value = "/".join(tokens) if tokens else None
    extra = {}
    with db.tx() as c:
        row = c.execute("SELECT status FROM raw_imports WHERE id=?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] == "imported":
            raise HTTPException(409, "已匯入正式表的列為唯讀，請先還原為 pending")
        c.execute(f"UPDATE raw_imports SET {field}=? WHERE id=?", (value, rid))
        # 商品名稱改變 → 若對應到主檔，自動同步敘述
        if field == "model" and value:
            prod = c.execute("SELECT description FROM products WHERE model=? LIMIT 1",
                             (value,)).fetchone()
            if prod:
                new_desc = prod["description"] or None
                c.execute("UPDATE raw_imports SET description=? WHERE id=?", (new_desc, rid))
                extra["description"] = new_desc
    return JSONResponse({"ok": True, "value": value, "extra": extra})


@app.post("/raw/{rid}/del")
def raw_del(rid: int):
    with db.tx() as c:
        c.execute("DELETE FROM raw_imports WHERE id=?", (rid,))
    return RedirectResponse("/raw", 303)


def _allocate_nonser_outs(bucket_lines: list, out_rows: list) -> dict:
    """對 bucket 內每筆非序號出貨配對源 inbound_line：
      1) 出貨明確指定 source_inbound_line_id 者，直接從該 line 扣
      2) 否則 FIFO（依進貨日 asc）
    回傳 {line_id: remaining_non_serial_qty}。"""
    chunks = {}
    for bl in bucket_lines:
        chunks[bl["id"]] = {
            "line_id": bl["id"],
            "date": bl["date"],
            "qty_left": max(0.0, float(bl["qty"]) - float(bl["total_ser"])),
        }
    fifo_sorted = sorted(chunks.values(), key=lambda x: (x["date"] or "", x["line_id"]))
    for ob in out_rows:
        remaining = float(ob["qty"])
        src_id = ob.get("source_inbound_line_id")
        if src_id and src_id in chunks and chunks[src_id]["qty_left"] > 0:
            take = min(remaining, chunks[src_id]["qty_left"])
            chunks[src_id]["qty_left"] -= take
            remaining -= take
        if remaining <= 0:
            continue
        for src in fifo_sorted:
            if remaining <= 0: break
            if src["qty_left"] <= 0: continue
            take = min(remaining, src["qty_left"])
            src["qty_left"] -= take
            remaining -= take
    return {lid: ch["qty_left"] for lid, ch in chunks.items()}


def _line_remaining(c, line_id: int) -> int:
    """計算單條 inbound_line 仍可被購物車取用的數量。
    = serial in_stock/returned + 配對後本 line 剩餘的非序號（同日優先 → FIFO）
    - 已加入購物車（但未結帳）的數量。"""
    line = c.execute("""SELECT id, product_id, qty, is_surplus, project_id, location_id
                        FROM inbound_lines WHERE id=?""", (line_id,)).fetchone()
    if not line:
        return 0
    pid = line["product_id"]; isu = line["is_surplus"]
    proj = line["project_id"]; loc = line["location_id"]
    avail_ser = c.execute("""SELECT COUNT(*) c FROM serial_items
                              WHERE inbound_line_id=?
                                AND status IN ('in_stock','returned')""",
                          (line_id,)).fetchone()["c"]
    bucket_lines = [dict(r) for r in c.execute("""
      SELECT il.id, il.qty, io.date,
             (SELECT COUNT(*) FROM serial_items WHERE inbound_line_id=il.id) total_ser
      FROM inbound_lines il JOIN inbound_orders io ON io.id=il.inbound_id
      WHERE il.product_id=?
        AND COALESCE(il.project_id,0)=COALESCE(?,0)
        AND il.is_surplus=?
        AND COALESCE(il.location_id,0)=COALESCE(?,0)
      ORDER BY io.date ASC, io.id ASC
    """, (pid, proj, isu, loc))]
    out_rows = [dict(r) for r in c.execute("""
      SELECT ol.qty, oo.date, ol.source_inbound_line_id
      FROM outbound_lines ol JOIN outbound_orders oo ON oo.id=ol.outbound_id
      WHERE ol.product_id=?
        AND COALESCE(ol.from_project_id,0)=COALESCE(?,0)
        AND ol.from_surplus=?
        AND COALESCE(ol.from_location_id,0)=COALESCE(?,0)
        AND NOT EXISTS (SELECT 1 FROM serial_items WHERE outbound_line_id=ol.id)
      ORDER BY oo.date ASC, oo.id ASC
    """, (pid, proj, isu, loc))]
    remaining_map = _allocate_nonser_outs(bucket_lines, out_rows)
    line_non_remaining = float(remaining_map.get(line_id, 0))
    # 扣已加入購物車的非序號量
    used_in_cart_non = c.execute("""SELECT COALESCE(SUM(qty),0) q FROM cart_items
                                     WHERE inbound_line_id=?
                                       AND serial_item_id IS NULL""",
                                 (line_id,)).fetchone()["q"] or 0
    line_non_remaining -= float(used_in_cart_non)
    if line_non_remaining < 0:
        line_non_remaining = 0
    serial_in_cart = c.execute("""SELECT COUNT(*) c FROM cart_items ci
                                   JOIN serial_items si ON si.id = ci.serial_item_id
                                   WHERE si.inbound_line_id=?""",
                               (line_id,)).fetchone()["c"]
    avail_ser_eff = max(0, int(avail_ser) - int(serial_in_cart))
    return int(avail_ser_eff) + int(line_non_remaining)


def _resolve_or_create(c, table, key_col, key_val, extra=None):
    if not key_val:
        return None
    row = c.execute(f"SELECT id FROM {table} WHERE {key_col}=?", (key_val,)).fetchone()
    if row:
        return row["id"]
    cols = [key_col] + list((extra or {}).keys())
    vals = [key_val] + list((extra or {}).values())
    return c.execute(
        f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        vals).lastrowid


@app.post("/raw/import-inbound")
async def raw_import_inbound(request: Request):
    """以 ITEM 為單位匯入：accepts form line_ids='1,2,3'（必同 ITEM、pending 狀態）。
    類型固定 'office'（台南辦公室）。
    多 project_no → 拒絕。
    回傳：{ok:true, inbound_id, lines, serials} 或 {ok:false, errors:[...]}.
    """
    from fastapi.responses import JSONResponse
    form = await request.form()
    raw_ids_str = form.get("line_ids", "")
    line_ids = [int(x) for x in raw_ids_str.split(",") if x.strip().isdigit()]
    if not line_ids:
        return JSONResponse({"ok": False, "errors": ["未提供 line_ids"]}, status_code=400)

    errors = []
    with db.tx() as c:
        placeholders = ",".join("?" * len(line_ids))
        rows = c.execute(f"SELECT * FROM raw_imports WHERE id IN ({placeholders})",
                         tuple(line_ids)).fetchall()
        rows = [dict(r) for r in rows]
        if len(rows) != len(line_ids):
            errors.append("部分 line 不存在")
        # 同 ITEM 驗證
        item_set = {r["item_no"] for r in rows}
        if len(item_set) > 1:
            errors.append("line 必須屬於同一 ITEM")
        # 全部要為 pending
        if any(r["status"] != "pending" for r in rows):
            errors.append("僅 pending 列可匯入")
        # 必填驗證
        for r in rows:
            if not r["model"]:
                errors.append(f"細項 #{r['id']}：缺商品名稱")
            if not r["qty"] or float(r["qty"] or 0) <= 0:
                errors.append(f"細項 #{r['id']}：數量未填或 <=0")
        # 日期：取群組內第一個有 date 者
        date_v = next((r["date"] for r in rows if r["date"]), None)
        if not date_v:
            errors.append("ITEM 內所有細項皆無日期，無法建立進貨單")
        # 多 project 允許：每行 inbound_line 各自帶 project_id；單頭 project 取首見值
        proj_set = {(r["project_no"] or "").strip() for r in rows}
        proj_set.discard("")
        single_proj = next((r["project_no"] for r in rows if r["project_no"]), None)
        # model → product 驗證
        prod_cache = {}
        for r in rows:
            if not r["model"]:
                continue
            p = c.execute("SELECT id, is_kit FROM products WHERE model=? LIMIT 1",
                          (r["model"],)).fetchone()
            if not p:
                errors.append(f"細項 #{r['id']}：型號「{r['model']}」不在料件主檔")
            else:
                prod_cache[r["id"]] = dict(p)
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)

        # 通過驗證 → 建立 inbound_order
        signer_id = None
        signer_name = next((r["signer"] for r in rows if r["signer"]), None)
        if signer_name:
            signer_id = _resolve_or_create(c, "staff", "name", signer_name)
        po_no = next((r["po_no"] for r in rows if r["po_no"]), None)
        proj_id = None
        if single_proj:
            owner = next((r["owner"] for r in rows if r["owner"]), None)
            pname = next((r["project_name"] for r in rows if r["project_name"]), None)
            proj_id = _resolve_or_create(c, "projects", "job_no", single_proj,
                                          {"owner": owner, "project_name": pname})
        photo_sent = 1 if date_v else 0  # 回傳日期 = raw.date

        # 供應商：以 ITEM 為單位 — 取群組內第一個非空 source
        order_sup_name = next((r["source"] for r in rows if r["source"]), None)
        order_sup_id = (_resolve_or_create(c, "suppliers", "name", order_sup_name)
                        if order_sup_name else None)
        cur = c.execute("""INSERT INTO inbound_orders(type, date, signer_id, po_no,
                            project_id, supplier_id, photo_sent, photo_sent_date)
                            VALUES('office', ?, ?, ?, ?, ?, ?, ?)""",
                        (date_v, signer_id, po_no, proj_id, order_sup_id,
                         photo_sent, date_v))
        inbound_id = cur.lastrowid

        lines_created = 0
        serials_created = 0
        for r in rows:
            p = prod_cache[r["id"]]
            pid = p["id"]
            qty = float(r["qty"])
            loc_id = _resolve_or_create(c, "locations", "code", r["location"]) if r["location"] else None
            # 供應商已寫到 inbound_orders 層；inbound_lines.supplier_id 留 NULL
            sup_id = None
            # raw 列若自身有 project_no 覆蓋（一般同 single_proj，但保險用各自值）
            line_proj_id = proj_id
            if r["project_no"]:
                line_proj_id = _resolve_or_create(c, "projects", "job_no", r["project_no"])

            page_no_v = r.get("page_no")
            line_po_no = (r["po_no"] or "").strip() or None if "po_no" in r.keys() else None
            cur2 = c.execute("""INSERT INTO inbound_lines(inbound_id, product_id, qty,
                                unit, location_id, is_surplus, project_id, supplier_id, page_no, po_no)
                                VALUES(?,?,?,?,?,0,?,?,?,?)""",
                             (inbound_id, pid, qty, "個",
                              loc_id, line_proj_id, sup_id, page_no_v, line_po_no))
            line_db_id = cur2.lastrowid
            lines_created += 1
            # 從 raw.serial_no 切分
            raw_sn = (r["serial_no"] or "").strip()
            if raw_sn:
                for tok in raw_sn.replace("\n", "/").replace(",", "/").replace("，", "/").split("/"):
                    norm = db.normalize_sn(tok)
                    if not norm:
                        continue
                    dup = c.execute("""SELECT id FROM serial_items
                                        WHERE product_id=? AND serial_no=?
                                          AND status IN ('in_stock','returned')""",
                                    (pid, norm)).fetchone()
                    if dup:
                        continue
                    c.execute("""INSERT INTO serial_items(product_id, serial_no, status,
                                 current_location_id, inbound_line_id, is_surplus, project_id)
                                 VALUES(?,?,?,?,?,0,?)""",
                              (pid, norm, "in_stock", loc_id, line_db_id, line_proj_id))
                    serials_created += 1
            # 標記 raw 列已匯入
            c.execute("""UPDATE raw_imports
                         SET status='imported', imported_ref_id=?, imported_at=CURRENT_TIMESTAMP
                         WHERE id=?""", (inbound_id, r["id"]))

    return JSONResponse({"ok": True, "inbound_id": inbound_id,
                         "lines": lines_created, "serials": serials_created})


@app.post("/raw/{rid}/dup")
def raw_dup(rid: int):
    """複製一筆 raw 列：新 id、status='pending'、清空 imported_ref_id/imported_at；其餘欄位全帶。"""
    with db.tx() as c:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(raw_imports)").fetchall()]
        copy_cols = [k for k in cols
                     if k not in ("id", "status", "imported_ref_id", "imported_at",
                                  "created_at", "note_internal")]
        src = c.execute("SELECT * FROM raw_imports WHERE id=?", (rid,)).fetchone()
        if not src:
            raise HTTPException(404)
        placeholders = ",".join("?" * (len(copy_cols) + 1))
        col_list = ",".join(copy_cols) + ",status"
        vals = [src[k] for k in copy_cols] + ["pending"]
        c.execute(f"INSERT INTO raw_imports({col_list}) VALUES({placeholders})", vals)
    return RedirectResponse("/raw", 303)


@app.post("/raw/clear")
def raw_clear():
    with db.tx() as c:
        c.execute("DELETE FROM raw_imports")
    return RedirectResponse("/raw", 303)


# ---------- 出貨購物車（cookie session）----------
def _cart_resolve_items(sess_id: str):
    """讀取目前 session 的購物車並 expand 成可顯示/結帳結構。
    回傳: list[ dict ] — 每筆含: id, kind('ser'|'nonser'), product_id, brand, model, description,
       qty, is_surplus, serial_no(若有), location_code, project_id, src_project_id, src_job_no,
       inbound_line_id(若無序號), inbound_id, inbound_date, available
    """
    items = []
    if not sess_id:
        return items
    rows = fetch_all("""
      SELECT ci.id, ci.serial_item_id, ci.inbound_line_id, ci.product_id, ci.qty,
             ci.is_surplus, ci.added_at, ci.note,
             p.model, p.description, b.name brand, p.base_unit
      FROM cart_items ci
      JOIN products p ON p.id = ci.product_id
      LEFT JOIN brands b ON b.id = p.brand_id
      WHERE ci.session_id = ?
      ORDER BY ci.added_at ASC, ci.id ASC
    """, (sess_id,))
    for r in rows:
        d = dict(r)
        d["kind"] = "ser" if d["serial_item_id"] else "nonser"
        d.update({"serial_no": None, "location_code": None, "location_id": None,
                  "project_id": None, "src_project_id": None, "src_job_no": None,
                  "inbound_id": None, "inbound_date": None, "job_no": None,
                  "available": True, "unavailable_reason": None})
        if d["kind"] == "ser":
            sr = fetch_one("""
              SELECT si.serial_no, si.status, si.project_id, si.is_surplus,
                     si.current_location_id, l.code loc,
                     il.inbound_id, io.date inbound_date, io.project_id io_pid,
                     io.type io_type, io.is_borrow_return,
                     pj.job_no, srcpj.job_no src_job_no, io.project_id src_pid
              FROM serial_items si
              LEFT JOIN locations l ON l.id = si.current_location_id
              LEFT JOIN inbound_lines il ON il.id = si.inbound_line_id
              LEFT JOIN inbound_orders io ON io.id = il.inbound_id
              LEFT JOIN projects pj ON pj.id = si.project_id
              LEFT JOIN projects srcpj ON srcpj.id = io.project_id
              WHERE si.id = ?
            """, (d["serial_item_id"],))
            if not sr:
                d["available"] = False
                d["unavailable_reason"] = "序號已不存在"
            else:
                d["serial_no"] = sr["serial_no"]
                d["location_code"] = sr["loc"]
                d["location_id"] = sr["current_location_id"]
                d["project_id"] = sr["project_id"]
                d["inbound_id"] = sr["inbound_id"]
                d["inbound_date"] = sr["inbound_date"]
                d["job_no"] = sr["job_no"]
                if sr["status"] not in ("in_stock", "returned"):
                    d["available"] = False
                    d["unavailable_reason"] = f"狀態 {sr['status']} 不可出貨"
                if (sr["io_type"] == "surplus_return" and not sr["is_borrow_return"]
                        and sr["src_pid"] and sr["project_id"] is None):
                    d["src_project_id"] = sr["src_pid"]
                    d["src_job_no"] = sr["src_job_no"]
        else:
            sr = fetch_one("""
              SELECT il.product_id, il.location_id, il.project_id, il.is_surplus,
                     l.code loc, io.id inbound_id, io.date inbound_date,
                     io.type io_type, io.is_borrow_return, io.project_id src_pid,
                     pj.job_no, srcpj.job_no src_job_no
              FROM inbound_lines il
              LEFT JOIN locations l ON l.id = il.location_id
              LEFT JOIN inbound_orders io ON io.id = il.inbound_id
              LEFT JOIN projects pj ON pj.id = il.project_id
              LEFT JOIN projects srcpj ON srcpj.id = io.project_id
              WHERE il.id = ?
            """, (d["inbound_line_id"],))
            if not sr:
                d["available"] = False
                d["unavailable_reason"] = "進貨明細已不存在"
            else:
                d["location_code"] = sr["loc"]
                d["location_id"] = sr["location_id"]
                d["project_id"] = sr["project_id"]
                d["inbound_id"] = sr["inbound_id"]
                d["inbound_date"] = sr["inbound_date"]
                d["job_no"] = sr["job_no"]
                if (sr["io_type"] == "surplus_return" and not sr["is_borrow_return"]
                        and sr["src_pid"] and sr["project_id"] is None):
                    d["src_project_id"] = sr["src_pid"]
                    d["src_job_no"] = sr["src_job_no"]
        items.append(d)
    return items


@app.get("/cart/count")
def cart_count(request: Request):
    from fastapi.responses import JSONResponse
    sid = get_sess(request)
    if not sid:
        return JSONResponse({"n": 0})
    n = fetch_one("SELECT COUNT(*) c FROM cart_items WHERE session_id=?", (sid,))["c"]
    return JSONResponse({"n": int(n)})


@app.get("/cart/mini")
def cart_mini(request: Request):
    """懸浮視窗用的簡易摘要：依來源分組。"""
    from fastapi.responses import JSONResponse
    sid = get_sess(request)
    items = _cart_resolve_items(sid) if sid else []
    sources = {}
    for it in items:
        sk = it.get("inbound_id") or 0
        s = sources.setdefault(sk, {
            "inbound_id": it.get("inbound_id"),
            "inbound_date": it.get("inbound_date"),
            "label": (f"#{it['inbound_id']} {it.get('inbound_date') or ''}".strip()
                      if it.get("inbound_id") else "庫存"),
            "items": [],
        })
        s["items"].append({
            "id": it["id"],
            "kind": it["kind"],
            "name": f"{it['brand'] or ''} {it['model']}".strip(),
            "qty": int(it["qty"]),
            "serial_no": it["serial_no"],
            "is_surplus": int(it["is_surplus"]),
            "available": it["available"],
        })
    src_list = sorted(sources.values(),
                      key=lambda s: (s["inbound_id"] is None, -(s["inbound_id"] or 0)))
    return JSONResponse({"n": len(items), "sources": src_list})


@app.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request):
    sid = get_sess(request)
    items = _cart_resolve_items(sid)
    # 大分類：來源（進貨單）。子分類：（product_id, is_surplus）
    sources = {}  # key: inbound_id or 0 (無來源)
    for it in items:
        sk = it.get("inbound_id") or 0
        s = sources.setdefault(sk, {
            "inbound_id": it.get("inbound_id"),
            "inbound_date": it.get("inbound_date"),
            "label": (f"來源：#{it['inbound_id']} {it.get('inbound_date') or ''}".strip()
                      if it.get("inbound_id") else "庫存（無進貨來源）"),
            "groups_map": {},
            "items_count": 0,
            "total_qty": 0.0,
        })
        s["items_count"] += 1
        s["total_qty"] += float(it["qty"])
        gk = (it["product_id"], int(it["is_surplus"]))
        g = s["groups_map"].setdefault(gk, {
            "product_id": it["product_id"],
            "is_surplus": int(it["is_surplus"]),
            "brand": it["brand"],
            "model": it["model"],
            "description": it["description"],
            "base_unit": it.get("base_unit"),
            "total_qty": 0.0,
            "ser_count": 0,
            "nonser_count": 0,
            "note": None,
            "cart_ids": [],
            "lines": [],
        })
        g["total_qty"] += float(it["qty"])
        g["cart_ids"].append(it["id"])
        if not g["note"] and (it.get("note") or "").strip():
            g["note"] = it["note"].strip()
        if it["kind"] == "ser":
            g["ser_count"] += 1
        else:
            g["nonser_count"] += int(it["qty"])
        g["lines"].append(it)
    # 攤平 groups_map → list；source 排序：有 inbound 在前依 id desc，0 放最後
    source_list = []
    for s in sources.values():
        s["groups"] = sorted(s["groups_map"].values(),
                             key=lambda g: ((g["brand"] or ""), g["model"]))
        del s["groups_map"]
        source_list.append(s)
    source_list.sort(key=lambda s: (s["inbound_id"] is None, -(s["inbound_id"] or 0)))
    projects = fetch_all("SELECT id, job_no, owner FROM projects ORDER BY job_no DESC")
    staff = fetch_all("SELECT id, name FROM staff ORDER BY name")
    return render(request, "cart.html", items=items, sources=source_list,
                  projects=projects, staff=staff)


@app.post("/cart/add")
async def cart_add(request: Request):
    sid = get_sess(request)
    if not sid:
        raise HTTPException(400, "session 異常，請重新整理頁面")
    form = await request.form()
    serial_ids = [int(x) for x in form.getlist("serial_ids") if x]
    nonser_picks = []
    raw_ns = form.get("nonser_pick") or ""
    if raw_ns:
        try:
            nonser_picks = json.loads(raw_ns)
        except Exception:
            nonser_picks = []
    added_ser = 0
    added_non = 0
    skipped_dup = 0
    with db.tx() as c:
        for sid_v in serial_ids:
            sr = c.execute("""SELECT id, product_id, is_surplus, status
                              FROM serial_items WHERE id=?""", (sid_v,)).fetchone()
            if not sr:
                continue
            if sr["status"] not in ("in_stock", "returned"):
                continue
            exist = c.execute("""SELECT id FROM cart_items
                                  WHERE session_id=? AND serial_item_id=?""",
                              (sid, sid_v)).fetchone()
            if exist:
                skipped_dup += 1
                continue
            c.execute("""INSERT INTO cart_items(session_id, serial_item_id, product_id,
                          qty, is_surplus) VALUES(?,?,?,?,?)""",
                      (sid, sid_v, sr["product_id"], 1.0, sr["is_surplus"]))
            added_ser += 1
        for np in nonser_picks:
            pid = int(np["pid"]); qty = float(np.get("qty") or 0)
            if qty <= 0:
                continue
            loc_id = int(np.get("loc") or 0) or None
            frompid = int(np.get("frompid") or 0) or None
            is_surplus_v = int(np.get("is_surplus") or 0)
            # 找對應 inbound_line（同 product / location / project / surplus，且仍有非序號可用量）
            cond_pid = "il.project_id IS NULL" if not frompid else "il.project_id = ?"
            params = [pid, is_surplus_v]
            if frompid:
                params.append(frompid)
            cond_loc = "il.location_id IS NULL" if loc_id is None else "il.location_id = ?"
            if loc_id is not None:
                params.append(loc_id)
            ils = c.execute(f"""
              SELECT il.id, il.qty,
                     (SELECT COUNT(*) FROM serial_items si
                      WHERE si.inbound_line_id = il.id
                        AND si.status IN ('in_stock','returned','shipped','returned_in')) sn_used
              FROM inbound_lines il
              WHERE il.product_id=? AND il.is_surplus=? AND {cond_pid} AND {cond_loc}
              ORDER BY il.id ASC
            """, tuple(params)).fetchall()
            remaining = qty
            for il in ils:
                if remaining <= 0:
                    break
                avail = float(il["qty"]) - float(il["sn_used"] or 0)
                # 扣掉購物車中已佔用本 line 的非序號量
                used_in_cart = c.execute("""SELECT COALESCE(SUM(qty),0) q FROM cart_items
                                             WHERE inbound_line_id=? AND serial_item_id IS NULL""",
                                          (il["id"],)).fetchone()["q"] or 0
                avail -= float(used_in_cart)
                if avail <= 0:
                    continue
                take = min(remaining, avail)
                c.execute("""INSERT INTO cart_items(session_id, inbound_line_id, product_id,
                              qty, is_surplus) VALUES(?,?,?,?,?)""",
                          (sid, il["id"], pid, take, is_surplus_v))
                remaining -= take
                added_non += take
    # 多種來源時統一回到 /cart
    return RedirectResponse(f"/cart?added_ser={added_ser}&added_non={int(added_non)}&dup={skipped_dup}", 303)


@app.post("/cart/quick-add")
async def cart_quick_add(request: Request):
    """快捷加入：accepts ?serial_item_id=X or ?inbound_line_id=Y&qty=Z"""
    sid = get_sess(request)
    form = await request.form()
    si_id = form.get("serial_item_id")
    il_id = form.get("inbound_line_id")
    qty = float(form.get("qty") or 1)
    return_to = form.get("return_to") or "/cart"
    with db.tx() as c:
        if si_id:
            si_id = int(si_id)
            sr = c.execute("""SELECT product_id, is_surplus, status
                              FROM serial_items WHERE id=?""", (si_id,)).fetchone()
            if not sr:
                raise HTTPException(404, "序號不存在")
            if sr["status"] not in ("in_stock", "returned"):
                raise HTTPException(409, f"序號狀態 {sr['status']} 不可加入")
            exist = c.execute("""SELECT id FROM cart_items
                                  WHERE session_id=? AND serial_item_id=?""",
                              (sid, si_id)).fetchone()
            if not exist:
                c.execute("""INSERT INTO cart_items(session_id, serial_item_id, product_id,
                              qty, is_surplus) VALUES(?,?,?,?,?)""",
                          (sid, si_id, sr["product_id"], 1.0, sr["is_surplus"]))
        elif il_id:
            il_id = int(il_id)
            il = c.execute("""SELECT product_id, qty, is_surplus FROM inbound_lines WHERE id=?""",
                           (il_id,)).fetchone()
            if not il:
                raise HTTPException(404, "進貨明細不存在")
            sn_used = c.execute("""SELECT COUNT(*) c FROM serial_items
                                    WHERE inbound_line_id=?""", (il_id,)).fetchone()["c"]
            used_in_cart = c.execute("""SELECT COALESCE(SUM(qty),0) q FROM cart_items
                                         WHERE inbound_line_id=? AND serial_item_id IS NULL""",
                                      (il_id,)).fetchone()["q"] or 0
            avail = float(il["qty"]) - float(sn_used) - float(used_in_cart)
            if avail <= 0:
                raise HTTPException(409, "此進貨行已無可加入購物車的數量")
            take = min(qty, avail)
            c.execute("""INSERT INTO cart_items(session_id, inbound_line_id, product_id,
                          qty, is_surplus) VALUES(?,?,?,?,?)""",
                      (sid, il_id, il["product_id"], take, il["is_surplus"]))
        else:
            raise HTTPException(400, "缺少 serial_item_id 或 inbound_line_id")
    return RedirectResponse(return_to, 303)


@app.post("/cart/add-line/{lid}")
def cart_add_line(lid: int, request: Request):
    """快速：將一個 inbound_line 上所有可加入項目（序號 + 剩餘無序號）一次加入購物車。"""
    sid = get_sess(request)
    if not sid:
        raise HTTPException(400, "session 異常")
    with db.tx() as c:
        il = c.execute("""SELECT id, product_id, qty, is_surplus
                          FROM inbound_lines WHERE id=?""", (lid,)).fetchone()
        if not il:
            raise HTTPException(404, "進貨明細不存在")
        # 1) 該 line 之 in_stock / returned 序號
        sers = c.execute("""SELECT id FROM serial_items
                            WHERE inbound_line_id=? AND status IN ('in_stock','returned')""",
                         (lid,)).fetchall()
        added_ser = 0
        for s in sers:
            exist = c.execute("""SELECT id FROM cart_items
                                  WHERE session_id=? AND serial_item_id=?""",
                              (sid, s["id"])).fetchone()
            if exist:
                continue
            c.execute("""INSERT INTO cart_items(session_id, serial_item_id, product_id,
                          qty, is_surplus) VALUES(?,?,?,?,?)""",
                      (sid, s["id"], il["product_id"], 1.0, il["is_surplus"]))
            added_ser += 1
        # 2) 剩餘非序號量（FIFO 配對 + 扣已加購物車）
        remaining = _line_remaining(c, lid)
        # 已加序號的就不要再算進非序號 qty（_line_remaining 已扣 serial_in_cart 與 ser_avail）
        non_part = max(0, remaining - added_ser)
        added_non = 0.0
        if non_part > 0:
            c.execute("""INSERT INTO cart_items(session_id, inbound_line_id, product_id,
                          qty, is_surplus) VALUES(?,?,?,?,?)""",
                      (sid, lid, il["product_id"], float(non_part), il["is_surplus"]))
            added_non = float(non_part)
    return RedirectResponse(
        f"/cart?added_ser={added_ser}&added_non={int(added_non)}&dup=0", 303)


@app.post("/cart/{cid}/del")
def cart_del(cid: int, request: Request):
    sid = get_sess(request)
    with db.tx() as c:
        c.execute("DELETE FROM cart_items WHERE id=? AND session_id=?", (cid, sid))
    return RedirectResponse("/cart", 303)


@app.post("/cart/{cid}/qty")
async def cart_update_qty(cid: int, request: Request):
    sid = get_sess(request)
    form = await request.form()
    try:
        new_qty = int(float(form.get("qty") or 0))
    except ValueError:
        raise HTTPException(400, "數量格式錯誤")
    if new_qty <= 0:
        raise HTTPException(400, "數量必須大於 0")
    with db.tx() as c:
        it = c.execute("""SELECT id, serial_item_id, inbound_line_id, qty
                          FROM cart_items WHERE id=? AND session_id=?""",
                       (cid, sid)).fetchone()
        if not it:
            raise HTTPException(404, "購物車項目不存在")
        if it["serial_item_id"] is not None:
            raise HTTPException(400, "序號項目固定為 1，不可調整")
        line_id = it["inbound_line_id"]
        if not line_id:
            raise HTTPException(400, "此項目無對應進貨來源，無法調整數量")
        remain = _line_remaining(c, line_id)
        max_allowed = int(remain) + int(it["qty"])
        if new_qty > max_allowed:
            raise HTTPException(409, f"超出可用數量（最多 {max_allowed}）")
        c.execute("UPDATE cart_items SET qty=? WHERE id=?", (new_qty, cid))
    return JSONResponse({"ok": True, "qty": new_qty})


@app.post("/cart/{cid}/note")
async def cart_update_note(cid: int, request: Request):
    sid = get_sess(request)
    form = await request.form()
    note = (form.get("note") or "").strip()
    with db.tx() as c:
        it = c.execute("SELECT id FROM cart_items WHERE id=? AND session_id=?",
                       (cid, sid)).fetchone()
        if not it:
            raise HTTPException(404, "購物車項目不存在")
        c.execute("UPDATE cart_items SET note=? WHERE id=?", (note or None, cid))
    return JSONResponse({"ok": True, "note": note})


@app.post("/cart/clear")
def cart_clear(request: Request):
    sid = get_sess(request)
    with db.tx() as c:
        c.execute("DELETE FROM cart_items WHERE session_id=?", (sid,))
    return RedirectResponse("/cart", 303)


@app.post("/cart/checkout")
async def cart_checkout(request: Request):
    sid = get_sess(request)
    if not sid:
        raise HTTPException(400, "session 異常")
    form = await request.form()
    to_project_id = int(form.get("project_id")) if form.get("project_id") else None
    date_v = form.get("date")
    if not date_v:
        raise HTTPException(400, "請填寫出貨日期")
    note = form.get("note") or ""
    signer_id = int(form.get("signer_id")) if form.get("signer_id") else None

    # 只結帳被勾選的項目
    pick_ids = [int(x) for x in form.getlist("cart_pick") if x]
    if not pick_ids:
        raise HTTPException(400, "請勾選至少一筆要出貨的項目")

    items = _cart_resolve_items(sid)
    chosen = [it for it in items if it["id"] in pick_ids]
    if not chosen:
        raise HTTPException(400, "勾選的項目已不在購物車中")
    unavail = [it for it in chosen if not it["available"]]
    if unavail:
        raise HTTPException(409, "下列項目不可用：" + "; ".join(
            f"#{it['id']} {it['model']}({it['unavailable_reason']})" for it in unavail))

    # 同一 is_surplus 才能一張單；分組
    surplus_set = {int(it["is_surplus"]) for it in chosen}
    if len(surplus_set) > 1:
        raise HTTPException(400, "選取項目混合了餘料與正常庫存，請分批結帳")
    is_surplus = surplus_set.pop()

    serial_ids = [it["serial_item_id"] for it in chosen if it["kind"] == "ser"]
    serial_notes = {it["serial_item_id"]: (it.get("note") or "").strip()
                    for it in chosen if it["kind"] == "ser" and (it.get("note") or "").strip()}
    nonser_picks = []
    # 為每個非序號 cart_item 收集使用者填入的序號（form 名為 cart_sn_{id}_{i}）
    # 序號為「可選」— 部分料件本身不追蹤序號，允許全部留空或部分填寫
    # 但若部分填寫，僅該幾筆會建立 serial_items；剩餘僅做數量扣帳
    for it in chosen:
        if it["kind"] != "nonser":
            continue
        n = int(it["qty"])
        provided = []
        for i in range(n):
            v = (form.get(f"cart_sn_{it['id']}_{i}") or "").strip()
            if v:
                provided.append(v)
        nonser_picks.append({
            "pid": it["product_id"],
            "loc": it["location_id"] or 0,
            "frompid": it["project_id"] or 0,
            "src": it["src_project_id"] or 0,
            "srcjob": it["src_job_no"] or "",
            "qty": it["qty"],
            "serials": provided,
            "src_inbound_line_id": it.get("inbound_line_id"),
            "note": (it.get("note") or "").strip(),
        })

    with db.tx() as c:
        out_id = _consume_serials_for_outbound(
            c, serial_ids,
            to_project_id=to_project_id, is_surplus=is_surplus, date_v=date_v,
            signer_id=signer_id, note=note,
            nonser_picks=nonser_picks, serial_notes=serial_notes,
        )
        # 結帳成功 → 刪掉這批 cart_items
        placeholders = ",".join("?" * len(pick_ids))
        c.execute(f"DELETE FROM cart_items WHERE session_id=? AND id IN ({placeholders})",
                  (sid, *pick_ids))
    return RedirectResponse(f"/outbound/{out_id}", 303)


# ---------- 工號轉移（已停用：保留路由占位以避免外部書籤 404） ----------
@app.get("/transfers", response_class=HTMLResponse)
def transfers_deprecated(request: Request):
    raise HTTPException(410, "「工號轉移」頁面已停用。請改用出貨（購物車結帳）流程。")


@app.get("/transfers/new", response_class=HTMLResponse)
def transfers_new_deprecated(request: Request):
    raise HTTPException(410, "「工號轉移」頁面已停用。請改用出貨（購物車結帳）流程。")


def _DEAD_transfers_new_form(request: Request,
                       mode: str = "project",
                       from_project_id: int = 0,
                       product_id: int = 0,
                       free_pool: int = 0):
    if mode not in ("project", "product"):
        mode = "project"
    products = rows_to_dicts(fetch_all("""
      SELECT p.id, p.model, p.base_unit, b.name brand
      FROM products p LEFT JOIN brands b ON b.id=p.brand_id
      WHERE p.is_kit=0 ORDER BY b.name, p.model
    """))
    projects = rows_to_dicts(fetch_all(
        "SELECT id, job_no, owner FROM projects ORDER BY job_no DESC"))

    slots = []
    if mode == "project" and (from_project_id or free_pool):
        sql = """
          SELECT sb.product_id, sb.project_id, sb.is_surplus, SUM(sb.qty) qty,
                 GROUP_CONCAT(DISTINCT l.code) locs,
                 b.name brand, p.model, p.description,
                 pj.job_no
          FROM stock_balance sb
          JOIN products p ON p.id = sb.product_id
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN locations l ON l.id = sb.location_id
          LEFT JOIN projects pj ON pj.id = sb.project_id
          WHERE sb.qty > 0
        """
        params = []
        if free_pool:
            sql += " AND sb.project_id IS NULL"
        else:
            sql += " AND sb.project_id = ?"
            params.append(from_project_id)
        if product_id:
            sql += " AND sb.product_id = ?"
            params.append(product_id)
        sql += """ GROUP BY sb.product_id, sb.project_id, sb.is_surplus
                   HAVING SUM(sb.qty) > 0
                   ORDER BY b.name, p.model """
        slots = rows_to_dicts(fetch_all(sql, params))
    elif mode == "product" and product_id:
        slots = rows_to_dicts(fetch_all("""
          SELECT sb.product_id, sb.project_id, sb.is_surplus, SUM(sb.qty) qty,
                 GROUP_CONCAT(DISTINCT l.code) locs,
                 b.name brand, p.model, p.description,
                 pj.job_no, pj.owner
          FROM stock_balance sb
          JOIN products p ON p.id = sb.product_id
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN locations l ON l.id = sb.location_id
          LEFT JOIN projects pj ON pj.id = sb.project_id
          WHERE sb.qty > 0 AND sb.product_id = ?
          GROUP BY sb.product_id, sb.project_id, sb.is_surplus
          HAVING SUM(sb.qty) > 0
          ORDER BY (sb.project_id IS NULL) DESC, pj.job_no
        """, (product_id,)))

    # 載入每個 slot 的序號清單
    serials_by_slot = {}
    if slots:
        pid_set = {s["product_id"] for s in slots}
        placeholders = ",".join("?" * len(pid_set))
        rows = fetch_all(f"""
          SELECT si.id, si.serial_no, si.product_id, si.project_id, si.is_surplus,
                 si.current_location_id, l.code loc,
                 io.date inbound_date, io.id inbound_id, io.type inbound_type
          FROM serial_items si
          LEFT JOIN locations l ON l.id = si.current_location_id
          LEFT JOIN inbound_lines il ON il.id = si.inbound_line_id
          LEFT JOIN inbound_orders io ON io.id = il.inbound_id
          WHERE si.status IN ('in_stock', 'returned')
            AND si.product_id IN ({placeholders})
          ORDER BY si.serial_no
        """, tuple(pid_set))
        for r in rows:
            key = (r["product_id"], r["project_id"], r["is_surplus"])
            serials_by_slot.setdefault(key, []).append(dict(r))
    # 依「有序號者置於上方」重排
    for s in slots:
        key = (s["product_id"], s["project_id"], s["is_surplus"])
        s["serial_count"] = len(serials_by_slot.get(key, []))
        s["non_serial_qty"] = max(0, int(s["qty"]) - s["serial_count"])
    if mode == "project":
        slots.sort(key=lambda s: (0 if s["serial_count"] > 0 else 1,
                                  (s["brand"] or ""), s["model"]))

    from datetime import date as _date
    src_project = None
    if mode == "project" and from_project_id:
        src_project = fetch_one("SELECT id, job_no, owner, project_name FROM projects WHERE id=?",
                                 (from_project_id,))
    selected_product = None
    if mode == "product" and product_id:
        selected_product = fetch_one("""SELECT p.id, p.model, p.description, b.name brand
                                        FROM products p LEFT JOIN brands b ON b.id=p.brand_id
                                        WHERE p.id=?""", (product_id,))
    ctx = {
        "mode": mode,
        "products": products,
        "projects": projects,
        "from_project_id": from_project_id,
        "product_id": product_id,
        "free_pool": free_pool,
        "slots": slots,
        "serials_by_slot": serials_by_slot,
        "today": _date.today().isoformat(),
        "src_project": src_project,
        "selected_product": selected_product,
    }
    return render(request, "transfer_new.html", **ctx)


@app.post("/transfers/new")
async def transfers_new_post_deprecated(request: Request):
    raise HTTPException(410, "「工號轉移」已停用。")


async def _DEAD_transfers_new_post(request: Request):
    form = await request.form()
    date_v = form.get("date") or None
    from_pid = int(form.get("from_project_id")) if form.get("from_project_id") else None
    to_pid = int(form.get("to_project_id")) if form.get("to_project_id") else None
    product_id = int(form.get("product_id")) if form.get("product_id") else None
    qty = float(form.get("qty") or 0)
    location_code = (form.get("location_code") or "").strip()
    is_surplus = 1 if form.get("is_surplus") else 0
    note = form.get("note") or None

    # 若使用者勾選了具體序號，以序號數量為準
    serial_ids = [int(x) for x in form.getlist("serial_ids") if x]
    if serial_ids:
        qty = float(len(serial_ids))

    if not date_v or not product_id or qty <= 0 or to_pid is None:
        raise HTTPException(400, "日期 / 料件 / 轉入工號 / 數量為必填")
    if from_pid == to_pid:
        raise HTTPException(400, "來源與目標工號相同，無需轉移")

    with db.tx() as c:
        loc_id = None
        if location_code:
            row = c.execute("SELECT id FROM locations WHERE code=?", (location_code,)).fetchone()
            if not row:
                raise HTTPException(400, f"位置 {location_code} 不存在")
            loc_id = row["id"]
        # 驗證 from 池可用量（限定 location 若有給）
        if loc_id is not None:
            row = c.execute("""SELECT COALESCE(SUM(qty),0) avail FROM stock_balance
                               WHERE product_id=? AND is_surplus=? AND location_id=?
                                 AND project_id IS ?""",
                            (product_id, is_surplus, loc_id, from_pid)).fetchone()
        else:
            row = c.execute("""SELECT COALESCE(SUM(qty),0) avail FROM stock_balance
                               WHERE product_id=? AND is_surplus=?
                                 AND project_id IS ?""",
                            (product_id, is_surplus, from_pid)).fetchone()
        # 上面 SQL 不能用 IS ? 對 NULL；改用 Python 判斷
        if from_pid is None:
            r2 = c.execute("""SELECT COALESCE(SUM(qty),0) avail FROM stock_balance
                              WHERE product_id=? AND is_surplus=? AND project_id IS NULL"""
                           + (" AND location_id=?" if loc_id else ""),
                           (product_id, is_surplus) + ((loc_id,) if loc_id else ())).fetchone()
        else:
            r2 = c.execute("""SELECT COALESCE(SUM(qty),0) avail FROM stock_balance
                              WHERE product_id=? AND is_surplus=? AND project_id=?"""
                           + (" AND location_id=?" if loc_id else ""),
                           (product_id, is_surplus, from_pid) + ((loc_id,) if loc_id else ())).fetchone()
        avail = r2["avail"] or 0
        if qty > avail:
            raise HTTPException(400, f"來源池可轉量 {avail}，不足需求 {qty}")

        # 寫 transfer
        c.execute("""INSERT INTO project_transfers(date, from_project_id, to_project_id,
                     product_id, qty, location_id, is_surplus, note)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (date_v, from_pid, to_pid, product_id, qty, loc_id, is_surplus, note))

        # 序號層級：更新指定序號的 project_id（若未勾選則 auto-pick）
        sn_status = ("in_stock", "returned")
        if serial_ids:
            # 驗證每個序號都屬於 from 池且狀態允許
            placeholders = ",".join("?" * len(serial_ids))
            rows = c.execute(
                f"""SELECT id, project_id, status, is_surplus FROM serial_items
                    WHERE id IN ({placeholders}) AND product_id=?""",
                (*serial_ids, product_id),
            ).fetchall()
            if len(rows) != len(serial_ids):
                raise HTTPException(400, "部分勾選的序號不存在或料件不符")
            for r in rows:
                if r["status"] not in sn_status:
                    raise HTTPException(409, f"序號 #{r['id']} 狀態 {r['status']} 不可轉移")
                if r["is_surplus"] != is_surplus:
                    raise HTTPException(409, f"序號 #{r['id']} 餘料屬性與表單不一致")
                if from_pid is None:
                    if r["project_id"] is not None:
                        raise HTTPException(409, f"序號 #{r['id']} 不在自由池")
                else:
                    if r["project_id"] != from_pid:
                        raise HTTPException(409, f"序號 #{r['id']} 不屬於來源工號")
            for sid in serial_ids:
                c.execute("UPDATE serial_items SET project_id=? WHERE id=?", (to_pid, sid))
        else:
            # 非序號 / 未勾選 → 自動挑選 N 筆（向下相容）
            if from_pid is None:
                rows = c.execute(f"""SELECT id FROM serial_items
                                     WHERE product_id=? AND is_surplus=? AND project_id IS NULL
                                       AND status IN ({','.join(['?']*len(sn_status))})
                                     {'AND current_location_id=?' if loc_id else ''}
                                     ORDER BY id LIMIT ?""",
                                  (product_id, is_surplus, *sn_status,
                                   *((loc_id,) if loc_id else ()),
                                   int(qty))).fetchall()
            else:
                rows = c.execute(f"""SELECT id FROM serial_items
                                     WHERE product_id=? AND is_surplus=? AND project_id=?
                                       AND status IN ({','.join(['?']*len(sn_status))})
                                     {'AND current_location_id=?' if loc_id else ''}
                                     ORDER BY id LIMIT ?""",
                                  (product_id, is_surplus, from_pid, *sn_status,
                                   *((loc_id,) if loc_id else ()),
                                   int(qty))).fetchall()
            for r in rows:
                c.execute("UPDATE serial_items SET project_id=? WHERE id=?", (to_pid, r["id"]))
    return RedirectResponse("/transfers", 303)


# ---------- 序號池查詢 API ----------
@app.get("/api/serials/by-pool")
def api_serials_by_pool(product_id: int, project_id: int = 0,
                        is_surplus: int = 0, free_pool: int = 0):
    """列出某料件在指定工號池（或自由池）中可轉移的序號。
    project_id=0 + free_pool=1 → 自由池
    project_id>0 → 該工號池
    僅回傳 in_stock / returned 狀態的序號（可動）
    """
    sql = """SELECT si.id, si.serial_no, si.status, si.current_location_id,
                    l.code loc
             FROM serial_items si
             LEFT JOIN locations l ON l.id = si.current_location_id
             WHERE si.product_id=? AND si.is_surplus=?
               AND si.status IN ('in_stock','returned')"""
    params = [product_id, is_surplus]
    if free_pool:
        sql += " AND si.project_id IS NULL"
    elif project_id:
        sql += " AND si.project_id=?"
        params.append(project_id)
    else:
        return {"serials": []}
    sql += " ORDER BY si.serial_no"
    rows = fetch_all(sql, params)
    return {"serials": [dict(r) for r in rows]}


# ---------- 庫存可用量 API ----------
@app.get("/api/stock/availability")
def api_stock_availability(product_id: int, is_surplus: int = 0,
                           to_project_id: int = 0, qty: float = 0):
    """回傳該料件「自由池可用量」與「其他工號可借量」。
    - to_project_id: 需求方工號 id（>0 表已選）；該工號自身的池視為「自有」，不算借
    - qty: 期望需求量；用來算 need_loan
    自由池 + 自有 → 不需借；不足時 need_loan = qty - (free + own)
    """
    free_row = fetch_one("""
      SELECT COALESCE(SUM(qty),0) qty FROM stock_balance
      WHERE product_id=? AND is_surplus=? AND project_id IS NULL AND qty>0
    """, (product_id, is_surplus))
    free = free_row["qty"] or 0

    own = 0
    if to_project_id:
        own_row = fetch_one("""
          SELECT COALESCE(SUM(qty),0) qty FROM stock_balance
          WHERE product_id=? AND is_surplus=? AND project_id=? AND qty>0
        """, (product_id, is_surplus, to_project_id))
        own = own_row["qty"] or 0

    loanable_params = [product_id, is_surplus]
    where_extra = ""
    if to_project_id:
        where_extra = " AND sb.project_id <> ?"
        loanable_params.append(to_project_id)
    loanable = fetch_all(f"""
      SELECT sb.project_id, pj.job_no, pj.owner, pj.project_name,
             SUM(sb.qty) qty
      FROM stock_balance sb
      JOIN projects pj ON pj.id = sb.project_id
      WHERE sb.product_id=? AND sb.is_surplus=? AND sb.project_id IS NOT NULL
        AND sb.qty > 0 {where_extra}
      GROUP BY sb.project_id
      HAVING SUM(sb.qty) > 0
      ORDER BY qty DESC, pj.job_no
    """, loanable_params)

    free_and_own = free + own
    need_loan = max(0, qty - free_and_own) if qty > 0 else 0
    return {
        "product_id": product_id,
        "is_surplus": is_surplus,
        "free": free,
        "own": own,
        "free_and_own": free_and_own,
        "need_loan": need_loan,
        "loanable_from": [dict(r) for r in loanable],
    }


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
      ORDER BY oo.date DESC, oo.id DESC
    """)
    return render(request, "outbound_list.html", rows=rows)


@app.get("/outbound/new", response_class=HTMLResponse)
def out_new_form(request: Request, type: str = "normal",
                 mode: str = "", from_project_id: int = 0,
                 product_id: int = 0, free_pool: int = 0):
    if type not in ("normal", "surplus_transfer"):
        raise HTTPException(400)
    is_surplus = 1 if type == "surplus_transfer" else 0
    # 專案出貨：強制「依料件」模式（不需依來源工號）
    if type == "normal":
        mode = "product"
    elif not mode:
        mode = "project"
    picker = _build_picker_ctx(mode, from_project_id, product_id, free_pool, is_surplus)
    ctx = {
        "type": type,
        "is_surplus": is_surplus,
        "staff": fetch_all("SELECT * FROM staff ORDER BY name"),
        "picker_base_url": "/outbound/new",
        "preserved_query": f"type={type}",
        "pick_label": "出貨",
        "hide_project_tab": (type == "normal"),
    }
    ctx.update(picker)
    return render(request, "outbound_form.html", **ctx)


@app.post("/outbound/new")
async def out_new_post_legacy(request: Request):
    """舊路徑已停用：請改走購物車。將表單轉發至 /cart/add 後導向 /cart。"""
    return RedirectResponse("/cart", 307)


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
    serials_by_line = {}
    sn_rows = fetch_all("""
      SELECT id, serial_no, outbound_line_id
      FROM serial_items
      WHERE outbound_line_id IN (SELECT id FROM outbound_lines WHERE outbound_id=?)
      ORDER BY serial_no
    """, (i,))
    for r in sn_rows:
        serials_by_line.setdefault(r["outbound_line_id"], []).append(dict(r))
    return render(request, "outbound_detail.html", h=head, lines=lines,
                  serials_by_line=serials_by_line)


@app.post("/outbound/{i}/line/{lid}/note")
def out_line_note(i: int, lid: int, note: str = Form("")):
    with db.tx() as c:
        line = c.execute(
            "SELECT id FROM outbound_lines WHERE id=? AND outbound_id=?", (lid, i)
        ).fetchone()
        if not line:
            raise HTTPException(404, "明細不存在")
        c.execute("UPDATE outbound_lines SET note=? WHERE id=?",
                  (note.strip() or None, lid))
    return RedirectResponse(f"/outbound/{i}", 303)


@app.post("/outbound/{i}/del")
def out_del(i: int):
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM outbound_orders WHERE id=?", (i,)).fetchone():
            raise HTTPException(404, "出貨單不存在")
        # 找出此單所有 outbound_lines 及其連動的 serial_items
        line_ids = [r["id"] for r in c.execute(
            "SELECT id FROM outbound_lines WHERE outbound_id=?", (i,)).fetchall()]
        # 檢查衝突：若某序號要復原成 in_stock/returned，但同 (product, SN) 已有其他活躍行 → 擋下
        conflicts = []
        for lid in line_ids:
            for si in c.execute("""SELECT si.id, si.product_id, si.serial_no, si.is_surplus,
                                          si.inbound_line_id
                                   FROM serial_items si WHERE si.outbound_line_id=?""",
                                (lid,)).fetchall():
                other = c.execute("""SELECT id FROM serial_items
                                     WHERE product_id=? AND serial_no=?
                                       AND id<>? AND status IN ('in_stock','returned')""",
                                  (si["product_id"], si["serial_no"], si["id"])).fetchone()
                if other:
                    conflicts.append(si["serial_no"])
        if conflicts:
            raise HTTPException(409,
                "以下序號目前另有活躍紀錄，無法回復本單狀態："
                + ", ".join(conflicts[:10])
                + (f"（共 {len(conflicts)} 筆）" if len(conflicts) > 10 else "")
                + "。請先處理該序號的活躍紀錄後再刪除。")
        # 逐行復原 serial_items
        for lid in line_ids:
            for si in c.execute("""SELECT id, is_surplus, inbound_line_id
                                   FROM serial_items WHERE outbound_line_id=?""",
                                (lid,)).fetchall():
                new_status = "returned" if si["is_surplus"] else "in_stock"
                loc_id = None
                if si["inbound_line_id"]:
                    r = c.execute("SELECT location_id FROM inbound_lines WHERE id=?",
                                  (si["inbound_line_id"],)).fetchone()
                    loc_id = r["location_id"] if r else None
                c.execute("""UPDATE serial_items
                             SET status=?, outbound_line_id=NULL, current_location_id=?
                             WHERE id=?""",
                          (new_status, loc_id, si["id"]))
            # borrow_records 依 outbound_line_id ON DELETE CASCADE，會在下一步隨 line 一起清掉
            c.execute("DELETE FROM outbound_lines WHERE id=?", (lid,))
        c.execute("DELETE FROM outbound_orders WHERE id=?", (i,))
    return RedirectResponse("/outbound", 303)


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
    # 主列：每個料件一列（合計）
    summary_sql = f"""
      SELECT sb.product_id, b.name brand, p.model, p.description,
             p.base_unit, p.safety_stock,
             SUM(sb.qty) qty,
             SUM(CASE WHEN sb.is_surplus=0 THEN sb.qty ELSE 0 END) qty_normal,
             SUM(CASE WHEN sb.is_surplus=1 THEN sb.qty ELSE 0 END) qty_surplus,
             COUNT(*) detail_count
      FROM stock_balance sb
      JOIN products p ON p.id=sb.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=sb.location_id
      WHERE {' AND '.join(where)}
      GROUP BY sb.product_id
      HAVING SUM(sb.qty) <> 0
      ORDER BY b.name, p.model
    """
    rows = fetch_all(summary_sql, params)
    # 細項：依 (location, is_surplus, project) 列出
    detail_sql = f"""
      SELECT sb.product_id, l.code loc, sb.is_surplus, sb.qty,
             COALESCE(pj.job_no, '（自由池）') pool_label,
             sb.project_id
      FROM stock_balance sb
      JOIN products p ON p.id=sb.product_id
      LEFT JOIN brands b ON b.id=p.brand_id
      LEFT JOIN locations l ON l.id=sb.location_id
      LEFT JOIN projects pj ON pj.id=sb.project_id
      WHERE {' AND '.join(where)}
      ORDER BY l.code, sb.is_surplus, pj.job_no
    """
    details = fetch_all(detail_sql, params)
    details_by_pid = {}
    for d in details:
        details_by_pid.setdefault(d["product_id"], []).append(dict(d))
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
    return render(request, "stock.html", rows=rows, details_by_pid=details_by_pid,
                  q=q, only_surplus=only_surplus, pending=pending)


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
    # 時間線：同 (product_id, serial_no) 的所有 serial_items（含當前）
    timeline = fetch_all("""
      SELECT si.id, si.status, si.inbound_line_id, si.outbound_line_id,
             io.id AS inbound_id, io.date AS inbound_date,
             oo.id AS outbound_id, oo.date AS outbound_date
      FROM serial_items si
      LEFT JOIN inbound_lines il ON il.id=si.inbound_line_id
      LEFT JOIN inbound_orders io ON io.id=il.inbound_id
      LEFT JOIN outbound_lines ol ON ol.id=si.outbound_line_id
      LEFT JOIN outbound_orders oo ON oo.id=ol.outbound_id
      WHERE si.product_id=? AND si.serial_no=?
      ORDER BY COALESCE(io.date,'1900-01-01'), si.id
    """, (s["product_id"], s["serial_no"]))
    return render(request, "serial_detail.html", s=s, inb=inb, out=out, timeline=timeline)


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
