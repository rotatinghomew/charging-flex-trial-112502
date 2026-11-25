"""价格版本归属。

规则：价格版本按生效时刻适用于新会话；已开始的会话不因为随后
调价而被重算。归属只取决于会话开始时刻和版本窗口，因此晚到的
新价格版本不会改变已开始会话的归属结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import DataQuality, PriceVersion
from .sessions import Episode


@dataclass(frozen=True)
class PriceAssignment:
    session_id: str
    price_version: str | None
    cny_per_kwh: float | None
    basis: str  # "start_time" 或 "no_version"
    note: str | None = None


def validate_timeline(versions: list[PriceVersion], dq: DataQuality) -> None:
    """检查版本窗口重叠；重叠不致命（按 starts_at 最新者优先），但必须显式记录。"""
    ordered = sorted((v for v in versions if v.starts_at is not None), key=lambda v: v.starts_at)  # type: ignore[arg-type]
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev.ends_at is None or nxt.starts_at < prev.ends_at:  # type: ignore[operator]
            dq.add(
                "price_window_overlap",
                f"价格版本 {prev.price_version} 与 {nxt.price_version} 窗口重叠，按 starts_at 最新者优先",
                prev.price_version,
            )


def effective_version(versions: list[PriceVersion], moment: datetime) -> PriceVersion | None:
    """返回 moment 时刻生效的版本；多个候选时取 starts_at 最新者。"""
    candidates = [
        v
        for v in versions
        if v.starts_at is not None and v.starts_at <= moment and (v.ends_at is None or moment < v.ends_at)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v.starts_at)  # type: ignore[arg-type]


def assign_segment_price(versions: list[PriceVersion], session_id: str, started_at: datetime) -> PriceAssignment:
    version = effective_version(versions, started_at)
    if version is None:
        return PriceAssignment(session_id, None, None, "no_version", "开始时刻不在任何价格版本窗口内")
    return PriceAssignment(session_id, version.price_version, version.cny_per_kwh, "start_time")


def assign_episode_prices(
    versions: list[PriceVersion], episodes: list[Episode]
) -> dict[str, dict[str, object]]:
    """按事件给出价格归属。

    事件级归属取事件开始时刻的版本；事件内部若因调价产生多个版本，
    已开始的片段保留原归属，只标记 mid_episode_price_change。
    """
    result: dict[str, dict[str, object]] = {}
    for ep in episodes:
        segment_assignments = [
            assign_segment_price(versions, seg.session_id, seg.started_at)  # type: ignore[arg-type]
            for seg in ep.segments
        ]
        versions_seen = [a.price_version for a in segment_assignments]
        distinct = {v for v in versions_seen if v is not None}
        result[ep.episode_id] = {
            "episode_id": ep.episode_id,
            "price_version": versions_seen[0],
            "cny_per_kwh": segment_assignments[0].cny_per_kwh,
            "basis": segment_assignments[0].basis,
            "mid_episode_price_change": len(distinct) > 1,
            "segment_assignments": [
                {
                    "session_id": a.session_id,
                    "price_version": a.price_version,
                    "cny_per_kwh": a.cny_per_kwh,
                    "basis": a.basis,
                    "note": a.note,
                }
                for a in segment_assignments
            ],
        }
    return result
