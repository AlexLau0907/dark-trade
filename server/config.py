"""
集中配置模块
"""

import os
import re

# ---- 路径配置 ----
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DATA_DIR = os.path.join(BASE_DIR, "RawData")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "finance.duckdb")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# ---- DuckDB 配置 ----
DUCKDB_READ_ONLY = False

# ---- 服务配置 ----
HOST = "127.0.0.1"
PORT = 8000
API_PREFIX = "/api/v1"

# ---- 分页配置 ----
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# ---- 数据导入配置 ----
RAW_DATA_ENCODING = "utf-8"

# ---- 字段映射: JSON 数字键 -> 数据库列名 ----
FIELD_MAPPING = {
    "3":  "market",
    "4":  "stock_code",
    "5":  "internal_code",
    "6":  "dark_pool_funds",
    "7":  "light_pool_funds",
    "8":  "main_net_inflow",
    "11": "dark_pool_activity",
    "13": "latest_score",
    "14": "score_change_pct",
    "16": "stock_name",
    "17": "industry",
    "18": "sector",
    "21": "ranking",
}

DB_COLUMNS = list(FIELD_MAPPING.values()) + ["trade_date", "file_name", "board_type", "pinyin_initials"]

ALLOWED_SORT_FIELDS = {
    "stock_code", "stock_name", "sector", "industry",
    "dark_pool_funds", "light_pool_funds", "main_net_inflow",
    "dark_pool_activity", "latest_score", "score_change_pct",
    "ranking", "trade_date",
}


def get_board_type(code: str) -> str:
    """根据股票代码前缀推导板块分类"""
    code = str(code)
    if re.match(r'^60[01345]', code):
        return "上海主板"
    elif re.match(r'^000|^001', code):
        return "深圳主板"
    elif re.match(r'^00[234]', code):
        return "中小板"
    elif re.match(r'^30[01]', code):
        return "创业板"
    elif re.match(r'^688', code):
        return "科创板"
    else:
        return "其他"
