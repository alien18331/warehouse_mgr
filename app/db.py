import sqlite3
from pathlib import Path
from contextlib import contextmanager

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "warehouse.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with tx() as conn:
        conn.executescript(sql)
        _migrate(conn)


def _migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(inbound_orders)").fetchall()}
    if "photo_sent" not in cols:
        conn.execute("ALTER TABLE inbound_orders ADD COLUMN photo_sent INTEGER NOT NULL DEFAULT 0")
    if "photo_sent_date" not in cols:
        conn.execute("ALTER TABLE inbound_orders ADD COLUMN photo_sent_date TEXT")
    if "extra_job_nos" not in cols:
        conn.execute("ALTER TABLE inbound_orders ADD COLUMN extra_job_nos TEXT")
    if "extra_suppliers" not in cols:
        conn.execute("ALTER TABLE inbound_orders ADD COLUMN extra_suppliers TEXT")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "is_kit" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN is_kit INTEGER NOT NULL DEFAULT 0")
    if "comment" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN comment TEXT")

    # 工號歸屬庫存（自由池 = project_id NULL；綁定工號 = project_id 有值）
    il_cols = {r["name"] for r in conn.execute("PRAGMA table_info(inbound_lines)").fetchall()}
    il_added = False
    if "project_id" not in il_cols:
        conn.execute("ALTER TABLE inbound_lines ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        il_added = True

    ol_cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbound_lines)").fetchall()}
    ol_added = False
    if "from_project_id" not in ol_cols:
        conn.execute("ALTER TABLE outbound_lines ADD COLUMN from_project_id INTEGER REFERENCES projects(id)")
        ol_added = True

    si_cols = {r["name"] for r in conn.execute("PRAGMA table_info(serial_items)").fetchall()}
    si_added = False
    if "project_id" not in si_cols:
        conn.execute("ALTER TABLE serial_items ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        si_added = True

    conn.execute("CREATE INDEX IF NOT EXISTS ix_inbound_lines_project ON inbound_lines(project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_outbound_lines_from_project ON outbound_lines(from_project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_serial_items_project ON serial_items(project_id)")

    # Backfill 既有資料（僅當欄位是這次新增的時候執行，避免每次啟動都跑）
    if il_added:
        conn.execute("""UPDATE inbound_lines
                        SET project_id = (
                          SELECT project_id FROM inbound_orders WHERE id = inbound_lines.inbound_id
                        )
                        WHERE project_id IS NULL""")
    if si_added:
        conn.execute("""UPDATE serial_items
                        SET project_id = (
                          SELECT io.project_id FROM inbound_lines il
                          JOIN inbound_orders io ON io.id = il.inbound_id
                          WHERE il.id = serial_items.inbound_line_id
                        )
                        WHERE project_id IS NULL AND inbound_line_id IS NOT NULL""")
    if ol_added:
        # 從序號軌跡推回來源工號；無序號軌跡的維持 NULL（歷史資料視為從自由池扣）
        conn.execute("""UPDATE outbound_lines
                        SET from_project_id = (
                          SELECT il.project_id
                          FROM serial_items si
                          JOIN inbound_lines il ON il.id = si.inbound_line_id
                          WHERE si.outbound_line_id = outbound_lines.id
                          LIMIT 1
                        )
                        WHERE from_project_id IS NULL
                          AND EXISTS (SELECT 1 FROM serial_items
                                      WHERE outbound_line_id = outbound_lines.id)""")

    # 供應商搬到明細層：每張進貨單可含多間供應商
    if "supplier_id" not in il_cols:
        conn.execute("ALTER TABLE inbound_lines ADD COLUMN supplier_id INTEGER REFERENCES suppliers(id)")
        # backfill：line 沿用 inbound_orders.supplier_id（單頭舊資料）
        conn.execute("""UPDATE inbound_lines
                        SET supplier_id = (
                          SELECT supplier_id FROM inbound_orders WHERE id = inbound_lines.inbound_id
                        )
                        WHERE supplier_id IS NULL""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_inbound_lines_supplier ON inbound_lines(supplier_id)")

    # 餘料一律進自由池：backfill 將既有 surplus 的 project_id 歸零
    # （inbound_orders.project_id 仍保留為「退回來源」軌跡）
    conn.execute("UPDATE inbound_lines SET project_id = NULL WHERE is_surplus = 1 AND project_id IS NOT NULL")
    conn.execute("UPDATE serial_items SET project_id = NULL WHERE is_surplus = 1 AND project_id IS NOT NULL")

    # 工號轉移表：在倉庫內把某料件從 A 工號池移到 B 工號池（料件實體未動）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_transfers (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          date TEXT NOT NULL,
          from_project_id INTEGER REFERENCES projects(id),
          to_project_id   INTEGER REFERENCES projects(id),
          product_id      INTEGER NOT NULL REFERENCES products(id),
          qty REAL NOT NULL,
          location_id INTEGER REFERENCES locations(id),
          is_surplus INTEGER NOT NULL DEFAULT 0,
          note TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_transfers_from ON project_transfers(from_project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_transfers_to ON project_transfers(to_project_id)")

    # 借出流程：outbound_orders 擴充欄位（op_kind: ship | borrow）
    oo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbound_orders)").fetchall()}
    if "op_kind" not in oo_cols:
        conn.execute("ALTER TABLE outbound_orders ADD COLUMN op_kind TEXT NOT NULL DEFAULT 'ship'")
    if "borrower_text" not in oo_cols:
        conn.execute("ALTER TABLE outbound_orders ADD COLUMN borrower_text TEXT")
    if "borrow_to_project_id" not in oo_cols:
        conn.execute("ALTER TABLE outbound_orders ADD COLUMN borrow_to_project_id INTEGER REFERENCES projects(id)")
    if "expected_return_date" not in oo_cols:
        conn.execute("ALTER TABLE outbound_orders ADD COLUMN expected_return_date TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_outbound_op_kind ON outbound_orders(op_kind)")

    # 進貨擴充：is_borrow_return（CHECK 限制 type，故另用旗標欄）
    io_cols = {r["name"] for r in conn.execute("PRAGMA table_info(inbound_orders)").fetchall()}
    if "is_borrow_return" not in io_cols:
        conn.execute("ALTER TABLE inbound_orders ADD COLUMN is_borrow_return INTEGER NOT NULL DEFAULT 0")

    # 借出對帳表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS borrow_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          outbound_line_id INTEGER REFERENCES outbound_lines(id) ON DELETE CASCADE,
          serial_item_id INTEGER REFERENCES serial_items(id),
          product_id INTEGER NOT NULL REFERENCES products(id),
          qty REAL NOT NULL DEFAULT 1,
          from_project_id INTEGER REFERENCES projects(id),
          to_project_id INTEGER REFERENCES projects(id),
          borrower_text TEXT,
          borrowed_at TEXT NOT NULL,
          expected_return_date TEXT,
          returned_inbound_line_id INTEGER REFERENCES inbound_lines(id),
          returned_at TEXT,
          note TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_borrow_from ON borrow_records(from_project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_borrow_to ON borrow_records(to_project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_borrow_returned ON borrow_records(returned_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_borrow_serial ON borrow_records(serial_item_id)")

    # 出貨購物車：依 cookie session 分群，可放序號項目或無序號 inbound_line 取用量
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cart_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          serial_item_id INTEGER REFERENCES serial_items(id) ON DELETE CASCADE,
          inbound_line_id INTEGER REFERENCES inbound_lines(id) ON DELETE CASCADE,
          product_id INTEGER NOT NULL REFERENCES products(id),
          qty REAL NOT NULL DEFAULT 1,
          is_surplus INTEGER NOT NULL DEFAULT 0,
          added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_cart_session ON cart_items(session_id)")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_cart_serial
                    ON cart_items(session_id, serial_item_id)
                    WHERE serial_item_id IS NOT NULL""")

    # 修補：serial_items.project_id 應與其 inbound_line 的 project_id 一致
    # （早期 in_line_serials 編輯未帶 project_id，造成歸屬不一致）
    # 僅修補仍在庫/退回狀態、非餘料、且 inbound_line 有 project_id 的序號
    conn.execute("""
        UPDATE serial_items
        SET project_id = (
          SELECT il.project_id FROM inbound_lines il
          WHERE il.id = serial_items.inbound_line_id
        )
        WHERE project_id IS NULL
          AND is_surplus = 0
          AND status IN ('in_stock','returned')
          AND inbound_line_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM inbound_lines il
                      WHERE il.id = serial_items.inbound_line_id
                        AND il.project_id IS NOT NULL
                        AND il.is_surplus = 0)
    """)

    # 重建 stock_balance VIEW：除進/出貨外，project_transfers 也納入（雙邊）
    conn.execute("DROP VIEW IF EXISTS stock_balance")
    conn.execute("""
        CREATE VIEW stock_balance AS
        SELECT product_id, location_id, is_surplus, project_id, SUM(qty) AS qty
        FROM (
          SELECT product_id, location_id, is_surplus, project_id, qty FROM inbound_lines
          UNION ALL
          SELECT product_id, from_location_id AS location_id, from_surplus AS is_surplus,
                 from_project_id AS project_id, -qty AS qty FROM outbound_lines
          UNION ALL
          -- 工號轉移：來源池扣
          SELECT product_id, location_id, is_surplus, from_project_id AS project_id, -qty AS qty
          FROM project_transfers
          UNION ALL
          -- 工號轉移：目標池加
          SELECT product_id, location_id, is_surplus, to_project_id AS project_id, qty
          FROM project_transfers
        )
        GROUP BY product_id, location_id, is_surplus, project_id
    """)


def seed_sample_data():
    """手動執行才會建立的範例資料。所有 INSERT 都加 OR IGNORE，
    永遠不會覆蓋既有資料。建議只在全新環境執行一次。

    執行方式： python -m app.seed
    """
    with tx() as conn:
        for b in ["AB", "Schneider", "OMRON", "Phoenix contact", "WJ", "RoHS"]:
            conn.execute("INSERT OR IGNORE INTO brands(name) VALUES(?)", (b,))
        for s in ["所羅門股份有限公司", "普得企業股份有限公司", "勁傑_碧蓮"]:
            conn.execute("INSERT OR IGNORE INTO suppliers(name) VALUES(?)", (s,))
        for st, role in [("陳令佳", "簽收"), ("Irene", "通知"), ("蔡培君", "請購"),
                         ("杜俊毅", "簽收"), ("鄭明昇", "簽收"), ("蔡明海", "簽收"),
                         ("陳又祺", "簽收")]:
            conn.execute("INSERT OR IGNORE INTO staff(name, role) VALUES(?,?)", (st, role))
        for code, name in [("倉庫右側", "主倉右側"), ("倉庫中間地板", "中央地板"),
                           ("1E右側", "1E 區右側"), ("2D-2左側", "2D-2 左"),
                           ("2D-2右側", "2D-2 右"), ("1D-1中間", "1D-1 中")]:
            conn.execute("INSERT OR IGNORE INTO locations(code, name) VALUES(?,?)", (code, name))
        for jn, ow, pn in [
            ("J114-12-396", "鋒霈環境", "TSMC20P2_CRS冰晶石系統儀電工程(PDP)"),
            ("J114-11-345", "兆聯", "Tsmc F5 LSR Revemping&擴增"),
            ("J114-10-280", "千附", "玉月光ASECL二園區製程排氣AAS排氣工程"),
            ("J115-05-192", "兆聯實業", "TSMC_F18P9_WWT+REC系統儀控工程"),
            ("J114-10-286", "兆聯", "TSMC_F18P3_臨時加藥櫃正式化擴充工程"),
        ]:
            conn.execute("INSERT OR IGNORE INTO projects(job_no, owner, project_name) VALUES(?,?,?)",
                         (jn, ow, pn))
        ab = conn.execute("SELECT id FROM brands WHERE name='AB'").fetchone()
        sch = conn.execute("SELECT id FROM brands WHERE name='Schneider'").fetchone()
        if ab and sch:
            ab_id, sch_id = ab[0], sch[0]
            for brand_id, model, desc, unit, sn in [
                (ab_id, "1769-ASCII", "ASCII 模組", "個", 0),
                (ab_id, "1769-CRL3", "COMPACTLOGIX EXPANSION CABLE", "個", 0),
                (ab_id, "1769-IQ32", "CompactLogix 32 Point Digital Input", "個", 0),
                (ab_id, "1769-OB32", "32 POINT 24VDC OUTPUT MODULE", "個", 0),
                (ab_id, "22C-D088A103", "PowerFlex 400 45kW (60Hp) AC變頻器", "台", 1),
                (ab_id, "22C-D142A103", "PowerFlex 400 75kW (100Hp) AC變頻器", "台", 1),
                (sch_id, "MGPM-5330", "集合式電錶 PM5330", "個", 0),
                (sch_id, "RXZE2M114", "插拔式繼電器插座", "個", 0),
                (sch_id, "RXM4AB2BD", "插拔式繼電器 Relay 25A", "個", 0),
            ]:
                conn.execute("""INSERT OR IGNORE INTO products(brand_id, model, description,
                                base_unit, track_by_serial) VALUES(?,?,?,?,?)""",
                             (brand_id, model, desc, unit, sn))


# 舊名稱保留為 alias，避免破壞外部呼叫
seed_if_empty = seed_sample_data
