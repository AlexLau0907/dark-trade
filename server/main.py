"""
FastAPI 应用入口 + 全部 API 路由
"""

import json
import math
import os
import re
import asyncio
from datetime import datetime, timedelta
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException, Path
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from .config import (
    API_PREFIX, HOST, PORT, STATIC_DIR,
    DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ALLOWED_SORT_FIELDS, get_board_type,
)
from .database import init_database, close_connection, get_connection
from .importer import run_import, get_db_status
from .models import (
    StockItem, PaginatedResponse, StockHistoryItem,
    SectorInfo, MarketSummary, DateInfo,
    ImportStatus, ReloadResult,
)


# ====== 应用生命周期 ======

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 60)
    print("  轻量级财经数据 API 服务器")
    print("=" * 60)
    init_database()
    status = get_db_status()
    if status["pending_files"] > 0:
        print(f"[Startup] 检测到 {status['pending_files']} 个新文件，开始导入...")
        run_import(full_reload=False)
    else:
        print(f"[Startup] 数据已是最新 ({status['total_rows']} 行, {status['date_range']})")
    print(f"[Startup] 前端页面: http://{HOST}:{PORT}")
    print(f"[Startup] Swagger:  http://{HOST}:{PORT}/docs")
    print("-" * 60)

    # 人气数据定时刷新: 每天00:30起每小时刷新一次
    last_hour = -1
    async def popularity_scheduler():
        nonlocal last_hour
        while True:
            await asyncio.sleep(30)
            now = datetime.now()
            if now.minute == 30 and now.hour != last_hour:
                last_hour = now.hour
                try:
                    print(f"[Scheduler] {now.strftime('%H:%M')} 刷新人气...")
                    _do_refresh_popularity()
                except Exception as e:
                    print(f"[Scheduler] 失败: {e}")

    task = asyncio.create_task(popularity_scheduler())
    yield
    task.cancel()
    close_connection()
    print("[Shutdown] 服务已关闭")


app = FastAPI(
    title="轻量级财经数据 API",
    description="基于 DuckDB 的灰马市场(东方财富)财经数据查询接口",
    version="1.1.0",
    lifespan=lifespan,
)

# 静态文件 + 前端首页
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    """前端首页（注入概念数据避免异步加载问题）"""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return {"message": "前端页面未部署", "docs": "/docs"}

    # 读取概念列表
    conn = get_connection()
    sectors_raw = conn.execute("""
        SELECT sector, COUNT(DISTINCT stock_code) AS sc
        FROM stocks_daily
        WHERE trade_date = (SELECT MAX(trade_date) FROM stocks_daily)
          AND sector IS NOT NULL AND sector != ''
        GROUP BY sector ORDER BY sc DESC
    """).fetchall()

    sectors_json = json.dumps(
        [{"name": r[0], "stock_count": r[1]} for r in sectors_raw],
        ensure_ascii=False,
    )

    html = open(index_path, "r", encoding="utf-8").read()
    # 在 <script> 开头注入数据
    html = html.replace(
        "// ====== Sector Filter ======",
        f"// ====== Sector Filter ======\nlet _preloadSectors = {sectors_json};",
    )

    return HTMLResponse(content=html, status_code=200)


# ====== 辅助函数 ======

def row_to_dict(row: tuple, columns: list) -> dict:
    return dict(zip(columns, row))


def row_to_stock_item(row: tuple, columns: list) -> StockItem:
    d = dict(zip(columns, row))
    return StockItem(
        market=d.get("market"),
        stock_code=d["stock_code"],
        internal_code=d.get("internal_code"),
        dark_pool_funds=d.get("dark_pool_funds"),
        light_pool_funds=d.get("light_pool_funds"),
        main_net_inflow=d.get("main_net_inflow"),
        dark_pool_activity=d.get("dark_pool_activity"),
        latest_score=d.get("latest_score"),
        score_change_pct=d.get("score_change_pct"),
        stock_name=d.get("stock_name"),
        industry=d.get("industry"),
        sector=d.get("sector"),
        ranking=d.get("ranking"),
        trade_date=str(d["trade_date"]) if d.get("trade_date") else None,
        board_type=d.get("board_type"),
        pinyin_initials=d.get("pinyin_initials"),
    )


def build_where_clause(
    code: str | None, name: str | None,
    sector: str | None, industry: str | None,
    trade_date: str | None, board_type: str | None,
) -> tuple[str, list]:
    conditions = []
    params = []

    if code:
        conditions.append("stock_code LIKE ?")
        params.append(f"%{code}%")
    if name:
        conditions.append("stock_name LIKE ?")
        params.append(f"%{name}%")
    if sector:
        conditions.append("sector LIKE ?")
        params.append(f"%{sector}%")
    if industry:
        conditions.append("industry LIKE ?")
        params.append(f"%{industry}%")
    if board_type:
        conditions.append("board_type = ?")
        params.append(board_type)
    if trade_date:
        conditions.append("trade_date BETWEEN ? AND ?")
        params.extend([trade_date, trade_date])
    else:
        conditions.append("trade_date BETWEEN (SELECT MAX(trade_date) FROM stocks_daily) AND (SELECT MAX(trade_date) FROM stocks_daily)")

    where = " WHERE " + " AND ".join(conditions)
    return where, params


# ====== 前端专用：轻量股票列表 ======

@app.get(f"{API_PREFIX}/stocks/list")
@app.get(f"{API_PREFIX}/stocks/list")
def get_stocks_lightweight():
    """返回所有股票最新数据 + 当日资金 + 人气排名"""
    conn = get_connection()
    rows = conn.execute("""
        WITH latest AS (
            SELECT MAX(trade_date) AS max_date FROM stocks_daily
        ),
        ranked_dates AS (
            SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
            FROM (SELECT DISTINCT trade_date FROM stocks_daily WHERE trade_date BETWEEN (SELECT MIN(trade_date) FROM stocks_daily) AND (SELECT max_date FROM latest)) t
        ),
        stock_latest AS (
            SELECT DISTINCT stock_code, stock_name, sector, board_type, pinyin_initials
            FROM stocks_daily WHERE trade_date BETWEEN (SELECT max_date FROM latest) AND (SELECT max_date FROM latest)
        ),
        sum_5 AS (
            SELECT stock_code, SUM(dark_pool_funds) AS sum_5d
            FROM stocks_daily WHERE trade_date IN (SELECT trade_date FROM ranked_dates WHERE rn <= 5) GROUP BY stock_code
        ),
        sum_10 AS (
            SELECT stock_code, SUM(dark_pool_funds) AS sum_10d
            FROM stocks_daily WHERE trade_date IN (SELECT trade_date FROM ranked_dates WHERE rn <= 10) GROUP BY stock_code
        ),
        sum_20 AS (
            SELECT stock_code, SUM(dark_pool_funds) AS sum_20d
            FROM stocks_daily WHERE trade_date IN (SELECT trade_date FROM ranked_dates WHERE rn <= 20) GROUP BY stock_code
        ),
        daily AS (
            SELECT stock_code, dark_pool_funds AS daily_flow
            FROM stocks_daily WHERE trade_date BETWEEN (SELECT max_date FROM latest) AND (SELECT max_date FROM latest)
        ),
        pop AS (
            SELECT stock_code, popularity_rank FROM popularity
            WHERE trade_date = (SELECT MAX(trade_date) FROM popularity)
        )
        SELECT s.stock_code, s.stock_name, s.sector, s.board_type, s.pinyin_initials,
            COALESCE(f5.sum_5d, 0), COALESCE(f10.sum_10d, 0), COALESCE(f20.sum_20d, 0),
            COALESCE(d.daily_flow, 0), p.popularity_rank
        FROM stock_latest s
        LEFT JOIN sum_5 f5 ON s.stock_code = f5.stock_code
        LEFT JOIN sum_10 f10 ON s.stock_code = f10.stock_code
        LEFT JOIN sum_20 f20 ON s.stock_code = f20.stock_code
        LEFT JOIN daily d ON s.stock_code = d.stock_code
        LEFT JOIN pop p ON s.stock_code = p.stock_code
        ORDER BY sum_20d DESC
    """).fetchall()
    return [{"stock_code":r[0],"stock_name":r[1],"sector":r[2],"board_type":r[3],"pinyin":r[4] or "","sum_5d":r[5],"sum_10d":r[6],"sum_20d":r[7],"daily_flow":r[8],"popularity":r[9]} for r in rows]



# ====== 5.1 股票查询 ======

@app.get(f"{API_PREFIX}/stocks", response_model=PaginatedResponse)
def get_stocks(
    code: str | None = Query(None, description="股票代码模糊搜索"),
    name: str | None = Query(None, description="股票名称模糊搜索"),
    sector: str | None = Query(None, description="概念板块筛选"),
    industry: str | None = Query(None, description="行业筛选"),
    trade_date: str | None = Query(None, description="交易日期 (YYYY-MM-DD)，默认最新"),
    board_type: str | None = Query(None, description="板块分类: 上海主板/深圳主板/中小板/创业板/科创板"),
    sort_by: str | None = Query(None, description="排序字段"),
    order: str = Query("desc", description="排序方向: asc/desc"),
    page: int = Query(1, ge=1),
    size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    conn = get_connection()
    size = min(size, MAX_PAGE_SIZE)

    if sort_by and sort_by not in ALLOWED_SORT_FIELDS:
        raise HTTPException(400, f"不支持的排序字段: {sort_by}")
    order_dir = "DESC" if order.lower() == "desc" else "ASC"

    where_clause, params = build_where_clause(code, name, sector, industry, trade_date, board_type)

    total = conn.execute(f"SELECT COUNT(*) FROM stocks_daily{where_clause}", params).fetchone()[0]
    pages = max(1, math.ceil(total / size))
    offset = (page - 1) * size

    order_clause = f" ORDER BY {sort_by} {order_dir}" if sort_by else " ORDER BY ranking ASC"
    data_sql = f"SELECT * FROM stocks_daily{where_clause}{order_clause} LIMIT ? OFFSET ?"
    rows = conn.execute(data_sql, params + [size, offset]).fetchall()
    columns = [desc[0] for desc in conn.description]

    return PaginatedResponse(
        total=total, page=page, size=size, pages=pages,
        items=[row_to_stock_item(row, columns) for row in rows],
    )


@app.get(f"{API_PREFIX}/stocks/{{code}}", response_model=StockItem)
def get_stock_detail(code: str = Path(..., min_length=6, max_length=6)):
    conn = get_connection()
    row = conn.execute(
        f"SELECT * FROM stocks_daily WHERE stock_code BETWEEN '{code}' AND '{code}' "
        f"AND trade_date BETWEEN (SELECT MAX(trade_date) FROM stocks_daily) AND (SELECT MAX(trade_date) FROM stocks_daily) LIMIT 1",
    ).fetchone()
    if not row:
        raise HTTPException(404, f"未找到股票: {code}")
    columns = [desc[0] for desc in conn.description]
    return row_to_stock_item(row, columns)


@app.get(f"{API_PREFIX}/stocks/{{code}}/history", response_model=list[StockHistoryItem])
def get_stock_history(code: str = Path(..., min_length=6, max_length=6)):
    conn = get_connection()
    rows = conn.execute(
        "SELECT trade_date, dark_pool_funds, light_pool_funds, main_net_inflow, "
        "dark_pool_activity, latest_score, score_change_pct, sector, ranking "
        f"FROM stocks_daily WHERE stock_code BETWEEN '{code}' AND '{code}' ORDER BY trade_date ASC",
    ).fetchall()
    return [
        StockHistoryItem(
            trade_date=str(r[0]), dark_pool_funds=r[1], light_pool_funds=r[2],
            main_net_inflow=r[3], dark_pool_activity=r[4], latest_score=r[5],
            score_change_pct=r[6], sector=r[7], ranking=r[8],
        )
        for r in rows
    ]


# ====== K线价格数据 (代理东财API) ======

import time as _time
_cache = {}  # {key: (expiry, data)}
_CACHE_TTL = 3600  # 缓存1小时

def _cached_get(url: str, params: dict, ttl: int = _CACHE_TTL):
    """带缓存的HTTP GET"""
    import hashlib
    key = hashlib.md5((url + str(sorted(params.items()))).encode()).hexdigest()
    now = _time.time()
    if key in _cache and _cache[key][0] > now:
        return _cache[key][1]
    resp = requests.get(url, params=params, timeout=15, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://quote.eastmoney.com/",
    })
    resp.raise_for_status()
    data = resp.json()
    _cache[key] = (now + ttl, data)
    # 限制缓存大小
    if len(_cache) > 200:
        oldest = min(_cache.items(), key=lambda x: x[1][0])
        del _cache[oldest[0]]
    return data

def _get_market_code(code: str) -> str:
    """股票代码 -> 东财市场代码 (1=SH, 0=SZ)"""
    if code.startswith(('6', '9')):
        return '1'
    return '0'


@app.get(f"{API_PREFIX}/stocks/{{code}}/kline")
def get_stock_kline(code: str = Path(..., min_length=6, max_length=6)):
    """获取股票日K线数据（价格走势），腾讯财经接口"""
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{prefix}{code},day,,,60,qfq"}
    try:
        data = _cached_get(url, params, ttl=1800)
        raw = data.get("data", {}).get(f"{prefix}{code}", {})
        klines = raw.get("qfqday", []) or raw.get("day", [])
        result = []
        for k in klines:
            if len(k) >= 6:
                result.append({
                    "date": k[0], "open": float(k[1]), "close": float(k[2]),
                    "high": float(k[3]), "low": float(k[4]),
                    "volume": float(k[5]),
                })
        return result
    except Exception:
        return []


# ====== 融资融券数据 (代理东财API) ======

@app.get(f"{API_PREFIX}/stocks/{{code}}/margin")
def get_stock_margin(code: str = Path(..., min_length=6, max_length=6)):
    """获取个股行情数据（腾讯财经接口，含市值/PE/换手率等）"""
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    url = "http://qt.gtimg.cn/q=" + prefix + code
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        text = resp.text
        match = text.split('"')[1] if '"' in text else ""
        if not match: return {}
        parts = match.split("~")
        return {
            "code": code,
            "name": parts[1] if len(parts) > 1 else "",
            "price": float(parts[3]) if len(parts) > 3 and parts[3] else None,
            "pre_close": float(parts[4]) if len(parts) > 4 and parts[4] else None,
            "open": float(parts[5]) if len(parts) > 5 and parts[5] else None,
            "volume": float(parts[6]) if len(parts) > 6 and parts[6] else None,
            "amount": float(parts[37]) if len(parts) > 37 and parts[37] else None,
            "change_pct": float(parts[32]) if len(parts) > 32 and parts[32] else None,
            "high": float(parts[33]) if len(parts) > 33 and parts[33] else None,
            "low": float(parts[34]) if len(parts) > 34 and parts[34] else None,
            "pe": float(parts[39]) if len(parts) > 39 and parts[39] else None,
            "total_mv": float(parts[45]) if len(parts) > 45 and parts[45] else None,
            "circ_mv": float(parts[44]) if len(parts) > 44 and parts[44] else None,
            "turnover": float(parts[38]) if len(parts) > 38 and parts[38] else None,
        }
    except Exception:
        return {}

# ====== 人气数据 ======

def _fetch_popularity() -> list[dict]:
    """从东财获取人气排名数据"""
    url = "https://data.eastmoney.com/dataapi/xuangu/list"
    params = {
        "st": "POPULARITY_RANK", "sr": "-1", "ps": "8000", "p": "1",
        "sty": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,NEW_PRICE,CHANGE_RATE,VOLUME_RATIO,HIGH_PRICE,LOW_PRICE,PRE_CLOSE_PRICE,VOLUME,DEAL_AMOUNT,TURNOVERRATE,POPULARITY_RANK",
        "source": "SELECT_SECURITIES", "client": "WEB", "hyversion": "v2",
    }
    resp = requests.get(url, params=params, timeout=15, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/",
    })
    data = resp.json()
    if data.get("result") and data["result"].get("data"):
        return data["result"]["data"]
    return []


@app.get(f"{API_PREFIX}/popularity/status")
def popularity_status():
    """查询人气数据刷新状态"""
    conn = get_connection()
    last = conn.execute(
        "SELECT refresh_time FROM popularity_refresh_log ORDER BY refresh_time DESC LIMIT 1"
    ).fetchone()
    count = conn.execute(
        "SELECT COUNT(*) FROM popularity WHERE trade_date = (SELECT MAX(trade_date) FROM popularity)"
    ).fetchone()[0]
    can_refresh = True
    if last:
        from datetime import datetime, timedelta
        ts = str(last[0])
        if '.' in ts: ts = ts[:ts.index('.')]
        last_time = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        if datetime.now() - last_time < timedelta(hours=1):
            can_refresh = False
    return {"can_refresh": can_refresh, "record_count": count, "last_refresh": str(last[0]) if last else None}


def _do_refresh_popularity():
    """执行人气数据刷新（内部调用，不检查限频）"""
    conn = get_connection()
    raw = _fetch_popularity()
    if not raw:
        return {"success": False, "record_count": 0}
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("DELETE FROM popularity WHERE trade_date = ?", [today])
    def _sf(v):
        try: return float(v) if v and v != '-' else None
        except: return None
    def _si(v):
        try: return int(v) if v and v != '-' else None
        except: return None
    rows = [[r.get("SECURITY_CODE",""), today, _si(r.get("POPULARITY_RANK")), _sf(r.get("NEW_PRICE")), _sf(r.get("CHANGE_RATE")), _sf(r.get("VOLUME_RATIO")), _sf(r.get("TURNOVERRATE")), _sf(r.get("VOLUME")), _sf(r.get("DEAL_AMOUNT"))] for r in raw]
    conn.executemany("INSERT INTO popularity (stock_code,trade_date,popularity_rank,new_price,change_rate,volume_ratio,turnover_rate,volume,deal_amount) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("DELETE FROM popularity WHERE trade_date < ?", [(datetime.now() - timedelta(days=21)).strftime("%Y-%m-%d")])
    conn.execute("INSERT INTO popularity_refresh_log (refresh_time, record_count) VALUES (CURRENT_TIMESTAMP, ?)", [len(rows)])
    conn.commit()
    return {"success": True, "record_count": len(rows), "date": today}


@app.post(f"{API_PREFIX}/popularity/refresh")
def refresh_popularity():
    """刷新当日人气数据（限频1小时，供前端调用）"""
    conn = get_connection()
    from datetime import datetime, timedelta
    last = conn.execute(
        "SELECT refresh_time FROM popularity_refresh_log ORDER BY refresh_time DESC LIMIT 1"
    ).fetchone()
    if last:
        ts = str(last[0])
        if '.' in ts: ts = ts[:ts.index('.')]
        last_time = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        if datetime.now() - last_time < timedelta(hours=1):
            raise HTTPException(429, "请在一个小时后刷新人气数据")
    raw = _fetch_popularity()
    if not raw:
        raise HTTPException(502, "获取人气数据失败")
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("DELETE FROM popularity WHERE trade_date = ?", [today])
    def _safe_float(v):
        try: return float(v) if v and v != '-' else None
        except: return None
    def _safe_int(v):
        try: return int(v) if v and v != '-' else None
        except: return None

    rows = []
    for r in raw:
        rows.append([
            r.get("SECURITY_CODE", ""), today,
            _safe_int(r.get("POPULARITY_RANK")), _safe_float(r.get("NEW_PRICE")),
            _safe_float(r.get("CHANGE_RATE")), _safe_float(r.get("VOLUME_RATIO")),
            _safe_float(r.get("TURNOVERRATE")), _safe_float(r.get("VOLUME")),
            _safe_float(r.get("DEAL_AMOUNT")),
        ])
    conn.executemany(
        "INSERT INTO popularity (stock_code,trade_date,popularity_rank,new_price,change_rate,volume_ratio,turnover_rate,volume,deal_amount) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    cutoff = (datetime.now() - timedelta(days=21)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM popularity WHERE trade_date < ?", [cutoff])
    conn.execute("INSERT INTO popularity_refresh_log (refresh_time, record_count) VALUES (CURRENT_TIMESTAMP, ?)", [len(rows)])
    conn.commit()
    return {"success": True, "record_count": len(rows), "date": today}


def get_sectors():
    conn = get_connection()
    rows = conn.execute("""
        SELECT sector, COUNT(DISTINCT stock_code), AVG(dark_pool_funds), AVG(score_change_pct)
        FROM stocks_daily
        WHERE trade_date = (SELECT MAX(trade_date) FROM stocks_daily)
          AND sector IS NOT NULL AND sector != ''
        GROUP BY sector ORDER BY COUNT(DISTINCT stock_code) DESC
    """).fetchall()
    return [
        SectorInfo(
            name=r[0], stock_count=r[1],
            avg_dark_pool_funds=round(r[2], 2) if r[2] else None,
            avg_score_change_pct=round(r[3], 4) if r[3] else None,
        )
        for r in rows
    ]


@app.get(f"{API_PREFIX}/sectors/{{name}}/stocks", response_model=PaginatedResponse)
def get_sector_stocks(
    name: str = Path(...),
    trade_date: str | None = Query(None),
    board_type: str | None = Query(None),
    sort_by: str | None = Query("ranking"),
    order: str = Query("asc"),
    page: int = Query(1, ge=1),
    size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    conn = get_connection()
    if sort_by not in ALLOWED_SORT_FIELDS:
        raise HTTPException(400, f"不支持的排序字段: {sort_by}")
    order_dir = "ASC" if order.lower() == "asc" else "DESC"

    conditions = ["sector = ?"]
    params = [name]
    if trade_date:
        conditions.append("trade_date BETWEEN ? AND ?")
        params.extend([trade_date, trade_date])
    else:
        conditions.append("trade_date BETWEEN (SELECT MAX(trade_date) FROM stocks_daily) AND (SELECT MAX(trade_date) FROM stocks_daily)")
    if board_type:
        conditions.append("board_type = ?")
        params.append(board_type)

    where = " WHERE " + " AND ".join(conditions)
    total = conn.execute(f"SELECT COUNT(*) FROM stocks_daily{where}", params).fetchone()[0]
    pages = max(1, math.ceil(total / size))
    offset = (page - 1) * size

    rows = conn.execute(
        f"SELECT * FROM stocks_daily{where} ORDER BY {sort_by} {order_dir} LIMIT ? OFFSET ?",
        params + [size, offset],
    ).fetchall()
    columns = [desc[0] for desc in conn.description]

    return PaginatedResponse(
        total=total, page=page, size=size, pages=pages,
        items=[row_to_stock_item(row, columns) for row in rows],
    )


# ====== 5.3 市场概览 ======

@app.get(f"{API_PREFIX}/market/summary", response_model=MarketSummary)
def get_market_summary():
    conn = get_connection()
    latest = conn.execute("SELECT MAX(trade_date) FROM stocks_daily").fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(DISTINCT stock_code) FROM stocks_daily WHERE trade_date = ?", [latest]
    ).fetchone()[0]
    sectors = conn.execute(
        "SELECT COUNT(DISTINCT sector) FROM stocks_daily WHERE trade_date = ? AND sector != ''", [latest]
    ).fetchone()[0]
    top = conn.execute("""
        SELECT sector, COUNT(DISTINCT stock_code), AVG(dark_pool_funds), AVG(score_change_pct)
        FROM stocks_daily WHERE trade_date = ? AND sector IS NOT NULL AND sector != ''
        GROUP BY sector ORDER BY COUNT(DISTINCT stock_code) DESC LIMIT 10
    """, [latest]).fetchall()
    return MarketSummary(
        trade_date=str(latest), total_stocks=total, sector_count=sectors,
        top_sectors=[
            SectorInfo(name=r[0], stock_count=r[1],
                       avg_dark_pool_funds=round(r[2], 2) if r[2] else None,
                       avg_score_change_pct=round(r[3], 4) if r[3] else None)
            for r in top
        ],
    )


@app.get(f"{API_PREFIX}/market/dates", response_model=list[DateInfo])
def get_dates():
    conn = get_connection()
    rows = conn.execute("""
        SELECT trade_date, COUNT(DISTINCT stock_code)
        FROM stocks_daily GROUP BY trade_date ORDER BY trade_date DESC
    """).fetchall()
    return [DateInfo(trade_date=str(r[0]), stock_count=r[1]) for r in rows]


# ====== 5.4 管理接口 ======

@app.get(f"{API_PREFIX}/admin/status", response_model=ImportStatus)
def admin_status():
    s = get_db_status()
    return ImportStatus(**s)


@app.post(f"{API_PREFIX}/admin/reload", response_model=ReloadResult)
def admin_reload(full: bool = Query(False, description="是否全量重导")):
    try:
        result = run_import(full_reload=full)
        return ReloadResult(
            success=True,
            message=f"导入完成: {result['new_files']} 个文件, {result['new_records']} 条记录",
            **result,
        )
    except Exception as e:
        raise HTTPException(500, f"导入失败: {str(e)}")
