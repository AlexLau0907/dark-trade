"""
Pydantic 请求/响应模型
"""

from pydantic import BaseModel, Field
from typing import Optional


# ====== 响应模型 ======

class StockItem(BaseModel):
    """股票单条数据"""
    market: Optional[int] = None
    stock_code: str
    internal_code: Optional[int] = None
    dark_pool_funds: Optional[int] = None
    light_pool_funds: Optional[int] = None
    main_net_inflow: Optional[int] = None
    dark_pool_activity: Optional[float] = None
    latest_score: Optional[float] = None
    score_change_pct: Optional[float] = None
    stock_name: Optional[str] = None
    industry: Optional[str] = None
    sector: Optional[str] = None
    ranking: Optional[int] = None
    trade_date: Optional[str] = None
    board_type: Optional[str] = None
    pinyin_initials: Optional[str] = None


class PaginatedResponse(BaseModel):
    """分页响应"""
    total: int
    page: int
    size: int
    pages: int
    items: list[StockItem]


class StockHistoryItem(BaseModel):
    """股票历史数据"""
    trade_date: str
    dark_pool_funds: Optional[int] = None
    light_pool_funds: Optional[int] = None
    main_net_inflow: Optional[int] = None
    dark_pool_activity: Optional[float] = None
    latest_score: Optional[float] = None
    score_change_pct: Optional[float] = None
    sector: Optional[str] = None
    ranking: Optional[int] = None


class SectorInfo(BaseModel):
    """板块信息"""
    name: str
    stock_count: int
    avg_dark_pool_funds: Optional[float] = None
    avg_score_change_pct: Optional[float] = None


class MarketSummary(BaseModel):
    """市场概览"""
    trade_date: str
    total_stocks: int
    sector_count: int
    top_sectors: list[SectorInfo]


class DateInfo(BaseModel):
    """交易日"""
    trade_date: str
    stock_count: int


class ImportStatus(BaseModel):
    """导入状态"""
    db_exists: bool
    total_rows: int
    date_range: Optional[str] = None
    imported_files: int
    raw_data_files: int
    pending_files: int


class ReloadResult(BaseModel):
    """重导结果"""
    success: bool
    message: str
    new_files: int = 0
    new_records: int = 0
    total_rows: int = 0


# ====== 请求模型 ======

class StockQuery(BaseModel):
    """股票查询参数"""
    code: Optional[str] = None
    name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    trade_date: Optional[str] = None
    sort_by: Optional[str] = None
    order: Optional[str] = "desc"
    page: int = Field(default=1, ge=1)
    size: int = Field(default=50, ge=1, le=200)
