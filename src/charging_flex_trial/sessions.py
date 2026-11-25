"""会话归并与电量解析。

业务规则：
- 同一 vehicle_key 的片段在时间相邻（间隔不超过 link_gap）时归并为一个
  充电事件（episode），允许跨站点；换站会被记录而不是被抹掉。
- vehicle_key 缺失的片段无法关联，只能保留为独立事件并标记不确定性，
  不得与其他片段强行合并。
- 缺少结束读数/结束时刻的片段电量未知：事件电量只给出已知下界，
  绝不能把未知当作零消耗。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import MeterReading, SessionSegment

DEFAULT_LINK_GAP = timedelta(minutes=30)


@dataclass(frozen=True)
class SegmentEnergy:
    session_id: str
    kwh: float | None  # None 表示未知（不是零）
    source: str  # meter / session_kwh / unknown
    note: str | None = None


@dataclass
class Episode:
    """同一车辆一次连续充电的事件视图。"""

    episode_id: str
    vehicle_key: str | None
    segments: list[SessionSegment] = field(default_factory=list)
    energies: list[SegmentEnergy] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)

    @property
    def started_at(self) -> datetime:
        return min(s.started_at for s in self.segments if s.started_at is not None)

    @property
    def ended_at(self) -> datetime | None:
        if any(s.ended_at is None for s in self.segments):
            return None
        return max(s.ended_at for s in self.segments)

    @property
    def is_closed(self) -> bool:
        return all(s.ended_at is not None for s in self.segments)

    @property
    def stations(self) -> list[str]:
        seen: list[str] = []
        for s in self.segments:
            if s.station_id not in seen:
                seen.append(s.station_id)
        return seen

    @property
    def switched_station(self) -> bool:
        return len(self.stations) > 1

    @property
    def energy_known_kwh(self) -> float:
        """已知片段电量之和，是事件真实电量的下界。"""
        return sum(e.kwh for e in self.energies if e.kwh is not None)

    @property
    def energy_complete(self) -> bool:
        """所有片段电量已知且事件闭合，电量才完整。"""
        return self.is_closed and all(e.kwh is not None for e in self.energies)

    @property
    def session_ids(self) -> list[str]:
        return [s.session_id for s in self.segments]


def resolve_energy(segment: SessionSegment, readings: dict[str, MeterReading]) -> SegmentEnergy:
    """解析单片段电量：优先表计读数，其次会话汇总值，否则未知。"""
    reading = readings.get(segment.session_id)
    if reading is not None:
        if reading.start_kwh is not None and reading.end_kwh is not None:
            if reading.end_kwh >= reading.start_kwh:
                return SegmentEnergy(segment.session_id, reading.end_kwh - reading.start_kwh, "meter")
            return SegmentEnergy(segment.session_id, None, "unknown", "读数倒走，电量不可用")
        # 有开始读数但缺结束读数：消耗未知，不是零
        return SegmentEnergy(segment.session_id, None, "unknown", "缺少结束读数")
    if segment.kwh is not None:
        return SegmentEnergy(segment.session_id, segment.kwh, "session_kwh")
    return SegmentEnergy(segment.session_id, None, "unknown", "无表计读数且无会话电量")


def build_episodes(
    segments: list[SessionSegment],
    readings: dict[str, MeterReading],
    link_gap: timedelta = DEFAULT_LINK_GAP,
) -> list[Episode]:
    """把会话片段归并为充电事件。

    归并键只有 vehicle_key；无法关联的片段保持独立并保留不确定性。
    时间重叠的同车片段不合规，不合并，分别标记。
    """
    episodes: list[Episode] = []
    by_vehicle: dict[str, list[SessionSegment]] = {}
    for seg in segments:
        if seg.vehicle_key:
            by_vehicle.setdefault(seg.vehicle_key, []).append(seg)
        else:
            ep = Episode(
                episode_id=f"ep-{seg.session_id}",
                vehicle_key=None,
                segments=[seg],
                energies=[resolve_energy(seg, readings)],
                uncertainties=["vehicle_not_linkable"],
            )
            episodes.append(ep)

    for vehicle_key, segs in sorted(by_vehicle.items()):
        ordered = sorted(segs, key=lambda s: s.started_at)  # type: ignore[arg-type]
        current: Episode | None = None
        for seg in ordered:
            if current is None:
                current = Episode(f"ep-{seg.session_id}", vehicle_key, [seg], [resolve_energy(seg, readings)])
                continue
            prev = current.segments[-1]
            reference_end = prev.ended_at if prev.ended_at is not None else prev.started_at
            gap = seg.started_at - reference_end  # type: ignore[operator]
            if gap < timedelta(0):
                # 同车时间重叠：不合规，保持独立以免重复计数被掩盖
                current.uncertainties.append("concurrent_segments_same_vehicle")
                episodes.append(current)
                current = Episode(f"ep-{seg.session_id}", vehicle_key, [seg], [resolve_energy(seg, readings)])
                current.uncertainties.append("concurrent_segments_same_vehicle")
                continue
            if gap <= link_gap:
                if prev.ended_at is None:
                    current.uncertainties.append("continuity_uncertain_open_segment")
                current.segments.append(seg)
                current.energies.append(resolve_energy(seg, readings))
            else:
                episodes.append(current)
                current = Episode(f"ep-{seg.session_id}", vehicle_key, [seg], [resolve_energy(seg, readings)])
        if current is not None:
            episodes.append(current)

    for ep in episodes:
        if not ep.is_closed:
            ep.uncertainties.append("open_segment")
        if any(e.kwh is None for e in ep.energies):
            ep.uncertainties.append("energy_incomplete")
        if ep.switched_station:
            ep.uncertainties.append("station_switch")
    return episodes
