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


def seed_if_empty():
    with tx() as conn:
        n = conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
        if n > 0:
            return
        brands = ["AB", "Schneider", "OMRON", "Phoenix contact", "WJ", "RoHS"]
        for b in brands:
            conn.execute("INSERT INTO brands(name) VALUES(?)", (b,))
        for s in ["所羅門股份有限公司", "普得企業股份有限公司", "勁傑_碧蓮"]:
            conn.execute("INSERT INTO suppliers(name) VALUES(?)", (s,))
        for st, role in [("陳令佳", "簽收"), ("Irene", "通知"), ("蔡培君", "請購"),
                         ("杜俊毅", "簽收"), ("鄭明昇", "簽收"), ("蔡明海", "簽收"),
                         ("陳又祺", "簽收")]:
            conn.execute("INSERT INTO staff(name, role) VALUES(?,?)", (st, role))
        for code, name in [("倉庫右側", "主倉右側"), ("倉庫中間地板", "中央地板"),
                           ("1E右側", "1E 區右側"), ("2D-2左側", "2D-2 左"),
                           ("2D-2右側", "2D-2 右"), ("1D-1中間", "1D-1 中")]:
            conn.execute("INSERT INTO locations(code, name) VALUES(?,?)", (code, name))
        for jn, ow, pn in [
            ("J114-12-396", "鋒霈環境", "TSMC20P2_CRS冰晶石系統儀電工程(PDP)"),
            ("J114-11-345", "兆聯", "Tsmc F5 LSR Revemping&擴增"),
            ("J114-10-280", "千附", "玉月光ASECL二園區製程排氣AAS排氣工程"),
            ("J115-05-192", "兆聯實業", "TSMC_F18P9_WWT+REC系統儀控工程"),
            ("J114-10-286", "兆聯", "TSMC_F18P3_臨時加藥櫃正式化擴充工程"),
        ]:
            conn.execute("INSERT INTO projects(job_no, owner, project_name) VALUES(?,?,?)", (jn, ow, pn))
        # 範例料件
        ab_id = conn.execute("SELECT id FROM brands WHERE name='AB'").fetchone()[0]
        sch_id = conn.execute("SELECT id FROM brands WHERE name='Schneider'").fetchone()[0]
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
            conn.execute(
                "INSERT INTO products(brand_id, model, description, base_unit, track_by_serial) VALUES(?,?,?,?,?)",
                (brand_id, model, desc, unit, sn),
            )
