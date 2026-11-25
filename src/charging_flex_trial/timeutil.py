"""时间解析与分桶工具。

所有时间戳都按站点本地时间处理（资料中 timezone 均为 Asia/Shanghai），
解析为 naive datetime；不在读取阶段补全或推断缺失时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta

ISO_MINUTE = "%Y-%m-%dT%H:%M"


def parse_ts(value: object) -> datetime | None:
    """把 ISO 分钟级时间戳解析为 naive datetime；缺失或非法时返回 None。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def iso(dt: datetime | None) -> str | None:
    """把 datetime 格式化回分钟级 ISO 字符串。"""
    if dt is None:
        return None
    return dt.strftime(ISO_MINUTE)


def bucket_start(dt: datetime, bucket_minutes: int) -> datetime:
    """返回 dt 所在分桶的起始时刻（从当日零点起对齐）。"""
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    offset = int((dt - midnight).total_seconds() // 60)
    return midnight + timedelta(minutes=(offset // bucket_minutes) * bucket_minutes)


def iter_buckets(start: datetime, end: datetime, bucket_minutes: int):
    """生成 [start, end) 内所有分桶起点，start 先向下对齐。"""
    current = bucket_start(start, bucket_minutes)
    step = timedelta(minutes=bucket_minutes)
    while current < end:
        yield current
        current += step


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """半开区间 [start, end) 是否相交。"""
    return a_start < b_end and b_start < a_end
