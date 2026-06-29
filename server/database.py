"""
DuckDB 连接管理 + 表初始化
"""

import os
import duckdb
from .config import DB_PATH, DATA_DIR

_connection: duckdb.DuckDBPyConnection | None = None


def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """获取 DuckDB 连接（单例模式）"""
    global _connection
    if _connection is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _connection = duckdb.connect(DB_PATH)
        _connection.execute("PRAGMA threads=4")
        _connection.execute("PRAGMA memory_limit='512MB'")
    return _connection


def init_database():
    """初始化数据库表结构（含板块分类 + 拼音首字母）"""
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS stocks_daily (
            market              TINYINT,
            stock_code          VARCHAR(6)    NOT NULL,
            internal_code       INTEGER,
            dark_pool_funds     BIGINT,
            light_pool_funds    BIGINT,
            main_net_inflow     BIGINT,
            dark_pool_activity  DOUBLE,
            latest_score        DOUBLE,
            score_change_pct    DOUBLE,
            stock_name          VARCHAR(50),
            industry            VARCHAR(50),
            sector              VARCHAR(50),
            ranking             INTEGER,
            trade_date          DATE          NOT NULL,
            file_name           VARCHAR(100)  NOT NULL,
            board_type          VARCHAR(20),
            pinyin_initials     VARCHAR(20),
            PRIMARY KEY (trade_date, stock_code, file_name)
        )
    """)

    # 兼容旧版：添加可能缺失的列
    for col, col_type in [
        ("board_type", "VARCHAR(20)"),
        ("pinyin_initials", "VARCHAR(20)"),
    ]:
        try:
            conn.execute(f"ALTER TABLE stocks_daily ADD COLUMN {col} {col_type}")
        except duckdb.Error:
            pass  # 列已存在

    conn.execute("""
        CREATE TABLE IF NOT EXISTS import_log (
            file_name    VARCHAR(100) PRIMARY KEY,
            import_time  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            record_count INTEGER
        )
    """)

    # 人气数据表（仅保留最近21个交易日）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS popularity (
            stock_code      VARCHAR(6) NOT NULL,
            trade_date      DATE NOT NULL,
            popularity_rank INTEGER,
            new_price       DOUBLE,
            change_rate     DOUBLE,
            volume_ratio    DOUBLE,
            turnover_rate   DOUBLE,
            volume          DOUBLE,
            deal_amount     DOUBLE,
            PRIMARY KEY (trade_date, stock_code)
        )
    """)

    # 人气刷新记录（限频1小时）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS popularity_refresh_log (
            refresh_time    TIMESTAMP PRIMARY KEY,
            record_count    INTEGER
        )
    """)

    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_code ON stocks_daily(stock_code)",
        "CREATE INDEX IF NOT EXISTS idx_sector ON stocks_daily(sector)",
        "CREATE INDEX IF NOT EXISTS idx_date ON stocks_daily(trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_ranking ON stocks_daily(trade_date, sector, ranking)",
    ]:
        try:
            conn.execute(idx_sql)
        except duckdb.Error:
            pass

    conn.commit()
    print("[DB] 数据库初始化完成")


def close_connection():
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
