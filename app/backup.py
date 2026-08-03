"""warehouse.db 每日備份 + 60 天清理。

機制：
- 每天 03:00 用 SQLite backup API 產生 backup/warehouse_YYYY-MM-DD.db（一天一份）
- 服務啟動時補跑：當天尚未備份就立刻備一份
- 備份後對新檔跑 PRAGMA integrity_check，通過才算成功
- 清理三道保險：
    1. 只刪 backup/ 內檔名完全符合 warehouse_YYYY-MM-DD.db 的檔案
    2. 無論多舊，最近 MIN_KEEP 份永不刪（防系統時鐘異常誤刪全部）
    3. 當天備份成功後才執行清理
"""
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

from .db import DB_PATH

BACKUP_DIR = DB_PATH.parent / "backup"
RETENTION_DAYS = 60
MIN_KEEP = 5
BACKUP_HOUR = 3  # 每天 03:00
_FNAME_RE = re.compile(r"^warehouse_(\d{4}-\d{2}-\d{2})\.db$")


def _log(msg: str):
    print(f"[BACKUP] {msg}", flush=True)


def _backup_once() -> bool:
    """備份今天一份；已存在則跳過。回傳是否「今天已有健康備份」。"""
    BACKUP_DIR.mkdir(exist_ok=True)
    dst_path = BACKUP_DIR / f"warehouse_{date.today().isoformat()}.db"
    if dst_path.exists():
        return True
    tmp_path = dst_path.with_suffix(".db.tmp")
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            dst.close()
            src.close()
        if not ok:
            _log(f"integrity_check 失敗，捨棄本次備份: {tmp_path.name}")
            tmp_path.unlink(missing_ok=True)
            return False
        tmp_path.rename(dst_path)
        _log(f"已備份 -> {dst_path.name} ({dst_path.stat().st_size // 1024} KB)")
        return True
    except Exception as e:
        _log(f"備份失敗: {e}")
        tmp_path.unlink(missing_ok=True)
        return False


def _cleanup():
    """刪除檔名日期超過 RETENTION_DAYS 的備份；最近 MIN_KEEP 份永不刪。"""
    entries = []  # (date_str, path)，僅收符合命名規則者
    for p in BACKUP_DIR.iterdir():
        m = _FNAME_RE.match(p.name)
        if m and p.is_file():
            entries.append((m.group(1), p))
    entries.sort(reverse=True)  # 新 -> 舊
    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    for date_str, p in entries[MIN_KEEP:]:
        if date_str < cutoff:
            try:
                p.unlink()
                _log(f"已清除過期備份: {p.name}")
            except Exception as e:
                _log(f"清除失敗 {p.name}: {e}")


def _run_cycle():
    if _backup_once():
        _cleanup()


def _scheduler_loop():
    while True:
        now = datetime.now()
        nxt = now.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        _run_cycle()


def start():
    _run_cycle()  # 啟動補跑：當天沒備份就立刻補
    threading.Thread(target=_scheduler_loop, daemon=True, name="db-backup").start()
    _log(f"排程已啟動（每日 {BACKUP_HOUR:02d}:00，保留 {RETENTION_DAYS} 天，至少 {MIN_KEEP} 份）")
