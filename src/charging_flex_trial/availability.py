"""充电桩状态对负荷分析与可预约容量的不同含义。

- maintenance（维护）：不可预约，也不构成负荷能力。
- derated（降功率）：仍可预约、仍承担负荷，但功率能力打折
  （derated_factor 是显式参数，会写入审计记录）。
- comms_lost（通讯中断）：真实状态未知。预约侧保守处理为不可预约；
  负荷侧不计入能力也不计入缺失，单独列为 unknown，避免把
  “观测不到”误当成“没有容量”或“容量正常”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import ChargerStatusEvent, DataQuality, Station
from .timeutil import iso, iter_buckets

DEFAULT_DERATED_FACTOR = 0.5


@dataclass(frozen=True)
class BucketCapacity:
    station_id: str
    bucket: datetime
    ports_total: int
    available: int
    derated: int
    maintenance: int
    comms_lost: int
    mixed_state: bool  # 桶内发生状态切换，归属存在近似

    @property
    def bookable(self) -> int:
        """可预约容量：维护与通讯中断都不可承诺。"""
        return self.available + self.derated

    @property
    def load_capable(self) -> int:
        """负荷分析口径的在运容量：通讯中断未知，不计入。"""
        return self.available + self.derated

    def effective_ports(self, derated_factor: float = DEFAULT_DERATED_FACTOR) -> float:
        return self.available + self.derated * derated_factor


@dataclass(frozen=True)
class PortAvailability:
    port_id: str
    available_minutes: float
    derated_minutes: float
    maintenance_minutes: float
    unknown_minutes: float  # 通讯中断：真实状态未知

    @property
    def usable_minutes(self) -> float:
        """真实可用时间（含降功率）。"""
        return self.available_minutes + self.derated_minutes


def port_roster(station: Station) -> list[str]:
    return [f"{station.station_id}-P{i}" for i in range(1, station.total_ports + 1)]


def _state_at(
    events: list[ChargerStatusEvent], moment: datetime, bucket_end: datetime
) -> tuple[str, bool]:
    """返回 moment 时刻的端口状态；若桶内存在状态边界则标记 mixed。"""
    state = "available"
    mixed = False
    for ev in events:
        starts = ev.starts_at
        ends = ev.ends_at
        if starts is not None and starts <= moment and (ends is None or moment < ends):
            state = ev.state or "available"
        # 事件边界落在 (moment, bucket_end) 内 → 桶内状态会变化
        for boundary in (starts, ends):
            if boundary is not None and moment < boundary < bucket_end:
                mixed = True
    return state, mixed


def station_capacity_timeline(
    station: Station,
    events: list[ChargerStatusEvent],
    window_start: datetime,
    window_end: datetime,
    bucket_minutes: int,
    dq: DataQuality,
) -> list[BucketCapacity]:
    roster = set(port_roster(station))
    by_port: dict[str, list[ChargerStatusEvent]] = {p: [] for p in roster}
    for ev in events:
        if ev.station_id != station.station_id:
            continue
        if ev.port_id not in roster:
            dq.add("unknown_port", f"站点 {station.station_id} 收到未知桩号 {ev.port_id} 的状态事件", ev.port_id)
        by_port.setdefault(ev.port_id, []).append(ev)

    timeline: list[BucketCapacity] = []
    step = timedelta(minutes=bucket_minutes)
    for bucket in iter_buckets(window_start, window_end, bucket_minutes):
        counts = {"available": 0, "derated": 0, "maintenance": 0, "comms_lost": 0}
        mixed = False
        for port_events in by_port.values():
            state, port_mixed = _state_at(port_events, bucket, bucket + step)
            counts[state if state in counts else "available"] += 1
            mixed = mixed or port_mixed
        timeline.append(
            BucketCapacity(
                station_id=station.station_id,
                bucket=bucket,
                ports_total=station.total_ports,
                available=counts["available"],
                derated=counts["derated"],
                maintenance=counts["maintenance"],
                comms_lost=counts["comms_lost"],
                mixed_state=mixed,
            )
        )
    return timeline


def port_available_time(
    station: Station,
    events: list[ChargerStatusEvent],
    window_start: datetime,
    window_end: datetime,
    dq: DataQuality,
    step_minutes: int = 5,
) -> list[PortAvailability]:
    """每个桩在窗口内的真实可用时间（按状态分钟数累计）。"""
    roster = port_roster(station)
    by_port: dict[str, list[ChargerStatusEvent]] = {p: [] for p in roster}
    for ev in events:
        if ev.station_id != station.station_id:
            continue
        if ev.port_id not in by_port:
            dq.add("unknown_port", f"站点 {station.station_id} 收到未知桩号 {ev.port_id} 的状态事件", ev.port_id)
        by_port.setdefault(ev.port_id, []).append(ev)

    result: list[PortAvailability] = []
    step = timedelta(minutes=step_minutes)
    for port_id in sorted(by_port):
        minutes = {"available": 0.0, "derated": 0.0, "maintenance": 0.0, "comms_lost": 0.0}
        moment = window_start
        while moment < window_end:
            state, _ = _state_at(by_port[port_id], moment, moment + step)
            minutes[state if state in minutes else "available"] += step_minutes
            moment += step
        result.append(
            PortAvailability(
                port_id=port_id,
                available_minutes=minutes["available"],
                derated_minutes=minutes["derated"],
                maintenance_minutes=minutes["maintenance"],
                unknown_minutes=minutes["comms_lost"],
            )
        )
    return result


def capacity_index(timeline: list[BucketCapacity]) -> dict[tuple[str, datetime], BucketCapacity]:
    return {(c.station_id, c.bucket): c for c in timeline}


def bucket_summary_rows(timeline: list[BucketCapacity]) -> list[dict[str, object]]:
    return [
        {
            "station_id": c.station_id,
            "bucket": iso(c.bucket),
            "ports_total": c.ports_total,
            "available": c.available,
            "derated": c.derated,
            "maintenance": c.maintenance,
            "comms_lost": c.comms_lost,
            "bookable": c.bookable,
            "load_capable": c.load_capable,
            "mixed_state": c.mixed_state,
        }
        for c in timeline
    ]
