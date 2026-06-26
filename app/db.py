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
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "is_kit" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN is_kit INTEGER NOT NULL DEFAULT 0")
    if "comment" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN comment TEXT")


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
