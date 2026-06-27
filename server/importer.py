"""
数据导入器: RawData JSON → DuckDB
支持全量导入和增量导入
"""

import os
import json
import re
import duckdb
from pypinyin import lazy_pinyin, Style
from .config import RAW_DATA_DIR, RAW_DATA_ENCODING, FIELD_MAPPING, DB_COLUMNS, get_board_type
from .database import get_connection


def parse_date_from_filename(filename: str) -> str | None:
    """从文件名提取日期: '20260511-01.json' -> '2026-05-11'"""
    m = re.match(r"(\d{4})(\d{2})(\d{2})", filename)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def get_pinyin_initials(name: str) -> str:
    """获取中文名称的拼音首字母: 澜起科技 -> LQKJ"""
    if not name:
        return ""
    try:
        py = lazy_pinyin(name, style=Style.FIRST_LETTER)
        return "".join(p.upper() for p in py if p)
    except Exception:
        return ""


def get_raw_files() -> list[str]:
    if not os.path.exists(RAW_DATA_DIR):
        return []
    return sorted(f for f in os.listdir(RAW_DATA_DIR) if f.endswith(".json"))


def get_imported_files() -> set[str]:
    conn = get_connection()
    try:
        result = conn.execute("SELECT file_name FROM import_log").fetchall()
        return {row[0] for row in result}
    except duckdb.Error:
        return set()


def import_file(filepath: str, filename: str) -> int:
    """导入单个 JSON 文件，返回记录数"""
    conn = get_connection()
    trade_date = parse_date_from_filename(filename)

    if trade_date is None:
        print(f"[Import] 无法解析日期: {filename}，跳过")
        return 0

    with open(filepath, "r", encoding=RAW_DATA_ENCODING, errors="replace") as f:
        data = json.load(f)

    if not isinstance(data, list) or len(data) == 0:
        return 0

    rows = []
    for item in data:
        row = {}
        for json_key, db_col in FIELD_MAPPING.items():
            val = item.get(json_key)
            if val == "" or val is None:
                row[db_col] = None
            else:
                row[db_col] = val
        row["trade_date"] = trade_date
        row["file_name"] = filename
        # 派生字段
        row["board_type"] = get_board_type(item.get("4", ""))
        row["pinyin_initials"] = get_pinyin_initials(item.get("16", ""))
        rows.append(row)

    if rows:
        columns_str = ", ".join(DB_COLUMNS)
        placeholders = ", ".join([f"${i+1}" for i in range(len(DB_COLUMNS))])
        conn.executemany(
            f"INSERT OR IGNORE INTO stocks_daily ({columns_str}) VALUES ({placeholders})",
            [tuple(r.get(c) for c in DB_COLUMNS) for r in rows],
        )

    return len(rows)


def mark_imported(filename: str, record_count: int):
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO import_log (file_name, import_time, record_count) VALUES (?, CURRENT_TIMESTAMP, ?)",
        [filename, record_count],
    )
    conn.commit()


def run_import(full_reload: bool = False) -> dict:
    conn = get_connection()
    all_files = get_raw_files()
    imported_files = get_imported_files() if not full_reload else set()
    new_files = [f for f in all_files if f not in imported_files]

    if full_reload:
        print(f"[Import] 全量重导模式: 清空已有数据...")
        conn.execute("DELETE FROM stocks_daily")
        conn.execute("DELETE FROM import_log")
        conn.commit()
        new_files = all_files

    print(f"[Import] RawData 文件总数: {len(all_files)}, 已导入: {len(imported_files)}, 待导入: {len(new_files)}")

    total_new_records = 0
    new_count = 0

    for filename in new_files:
        filepath = os.path.join(RAW_DATA_DIR, filename)
        try:
            record_count = import_file(filepath, filename)
            mark_imported(filename, record_count)
            total_new_records += record_count
            new_count += 1
            if new_count % 200 == 0:
                print(f"  [Import] 进度: {new_count}/{len(new_files)} 文件, {total_new_records} 条记录")
        except Exception as e:
            print(f"  [Import] 错误: {filename} - {e}")

    conn.commit()
    total_rows = conn.execute("SELECT COUNT(*) FROM stocks_daily").fetchone()[0]
    result = {"new_files": new_count, "new_records": total_new_records, "total_rows": total_rows}
    print(f"[Import] 完成: {new_count} 个新文件, {total_new_records} 条新记录, 总计 {total_rows} 行")
    return result


def get_db_status() -> dict:
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM stocks_daily").fetchone()
        total_rows = total[0] if total else 0
        dr = conn.execute("SELECT MIN(trade_date), MAX(trade_date) FROM stocks_daily").fetchone()
        date_range = None
        if dr and dr[0] and dr[1]:
            date_range = f"{dr[0]} ~ {dr[1]}"
        imported = conn.execute("SELECT COUNT(*) FROM import_log").fetchone()
        imported_count = imported[0] if imported else 0
        raw_count = len(get_raw_files())
        return {
            "db_exists": True, "total_rows": total_rows, "date_range": date_range,
            "imported_files": imported_count, "raw_data_files": raw_count,
            "pending_files": raw_count - imported_count,
        }
    except duckdb.Error:
        return {
            "db_exists": False, "total_rows": 0, "date_range": None,
            "imported_files": 0, "raw_data_files": len(get_raw_files()),
            "pending_files": len(get_raw_files()),
        }
