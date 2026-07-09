"""SpaceX 指标 dashboard：Flask 服务 + 后台刷新线程 + SQLite 快照历史。"""
import json
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, send_from_directory

import engine

_ET = ZoneInfo("America/New_York")


def _is_trading_ts(ts):
    """epoch 秒是否落在美股常规交易时段（美东周一-五 9:30-16:00，自动处理夏令时）。"""
    et = datetime.fromtimestamp(ts, _ET)
    if et.weekday() >= 5:
        return False
    m = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= m < 16 * 60

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "data" / "snapshots.db"
QUOTE_INTERVAL = 20          # 行情刷新（秒）
HISTORY_INTERVAL = 30 * 60   # 日线历史刷新（秒）

app = Flask(__name__, static_folder=str(BASE / "static"))
_state = {"latest": None, "error": None}
_lock = threading.Lock()


def _db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        ts INTEGER PRIMARY KEY, spcx REAL, sentiment REAL,
        dxyz_premium REAL, basket_residual REAL, payload TEXT)""")
    return conn


def _save_snapshot(result: dict):
    conn = _db()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
            (int(time.time()),
             result["spcx"]["last"],
             result["sentiment"]["score"],
             result["dxyz"]["premium"],
             result["basket"]["residual_today"],
             json.dumps(result)))
    conn.close()


def _seed_backfill(hist):
    """用 intraday 回算最近 2 天情绪序列灌入 SQLite，让走势图开箱有历史。
    INSERT OR IGNORE：已有点（含实时采样）不被覆盖。"""
    try:
        rows = engine.backfill_intraday(hist)
        conn = _db()
        with conn:
            # backfill 只填实时采集开始之前的历史段；实时段纯用实时点，
            # 避免两条计算路径（intraday 回算 vs 实时）密集交替成锯齿
            # 清理盘后/休市时段记录的实时点（平段），曲线只保留交易时段
            for (ts,) in conn.execute("SELECT ts FROM snapshots WHERE payload != ''").fetchall():
                if not _is_trading_ts(ts):
                    conn.execute("DELETE FROM snapshots WHERE ts = ?", (ts,))
            row = conn.execute("SELECT MIN(ts) FROM snapshots WHERE payload != ''").fetchone()
            earliest_rt = row[0] if row and row[0] else None
            conn.execute("DELETE FROM snapshots WHERE payload = ''")
            for r in rows:
                if r["sentiment"] is None:
                    continue
                if earliest_rt is not None and r["ts"] >= earliest_rt:
                    continue
                if not _is_trading_ts(r["ts"]):
                    continue
                conn.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?)",
                             (int(r["ts"]), r["spcx"], r["sentiment"],
                              r["dxyz_premium"], None, ""))
        conn.close()
        return len(rows)
    except Exception:
        return 0


def _load_series(hours: int = 168, max_points: int = 1500):
    conn = _db()
    rows = conn.execute(
        "SELECT ts, spcx, sentiment, dxyz_premium FROM snapshots "
        "WHERE ts > ? ORDER BY ts", (int(time.time()) - hours * 3600,)).fetchall()
    conn.close()
    if len(rows) > max_points:   # 降采样防点过多卡顿，保留最后一个点
        step = len(rows) // max_points + 1
        sampled = rows[::step]
        if sampled[-1] is not rows[-1]:
            sampled.append(rows[-1])
        rows = sampled
    return {
        "ts": [r[0] for r in rows],
        "spcx": [r[1] for r in rows],
        "sentiment": [r[2] for r in rows],
        "dxyz_premium": [r[3] for r in rows],
    }


def _refresh_loop():
    hist = None
    shares = None
    hist_at = 0.0
    while True:
        try:
            now = time.time()
            if hist is None or now - hist_at > HISTORY_INTERVAL:
                hist = engine.fetch_history()
                shares = engine.fetch_shares()
                _seed_backfill(hist)
                hist_at = now
            quotes = engine.fetch_quotes(shares)
            result = engine.compute(quotes, hist)
            with _lock:
                _state["latest"] = result
                _state["error"] = None
            if _is_trading_ts(now):   # 盘后/休市不记录历史点，曲线不出现平段
                _save_snapshot(result)
        except Exception:
            with _lock:
                _state["error"] = traceback.format_exc(limit=3)
        time.sleep(QUOTE_INTERVAL)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/data")
def api_data():
    with _lock:
        latest, error = _state["latest"], _state["error"]
    if latest is None:
        return jsonify({"status": "warming_up", "error": error}), 503
    return jsonify({"status": "ok", "data": latest,
                    "series": _load_series(), "error": error})


if __name__ == "__main__":
    import os
    threading.Thread(target=_refresh_loop, daemon=True).start()
    app.run(host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", 8500)))
