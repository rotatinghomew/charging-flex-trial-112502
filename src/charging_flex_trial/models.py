"""领域对象与原始记录解析。

解析只做结构转换和显式校验，不填补缺失值：
缺失、部分质量和待处理状态是资料含义的一部分。
无法识别的取值进入 data_quality 告警，而不是被静默改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .timeutil import parse_ts

SESSION_STATUSES = {"complete", "interrupted", "active"}
RESERVATION_STATUSES = {"proposed", "confirmed", "completed", "cancelled"}
CHARGER_STATES = {"available", "maintenance", "derated", "comms_lost"}


@dataclass
class DataQuality:
    """解析阶段发现的资料质量问题。"""

    issues: list[dict[str, Any]] = field(default_factory=list)

    def add(self, code: str, message: str, ref: str) -> None:
        self.issues.append({"code": code, "message": message, "ref": ref})

    def extend(self, other: "DataQuality") -> None:
        self.issues.extend(other.issues)


@dataclass(frozen=True)
class Station:
    station_id: str
    district: str | None
    total_ports: int
    timezone: str | None


@dataclass(frozen=True)
class SessionSegment:
    """一次充电会话片段；断电拆段或换站会产生同一车辆的多条记录。"""

    session_id: str
    vehicle_key: str | None
    station_id: str
    started_at: datetime | None
    ended_at: datetime | None
    kwh: float | None
    status: str | None

    @property
    def is_open(self) -> bool:
        """缺少结束时刻的片段视为未闭合，不能假定其结束时间。"""
        return self.ended_at is None


@dataclass(frozen=True)
class MeterReading:
    """电量读数；缺少结束读数时电量未知，不能当作零消耗。"""

    session_id: str
    start_kwh: float | None
    end_kwh: float | None


@dataclass(frozen=True)
class PriceVersion:
    price_version: str
    starts_at: datetime | None
    ends_at: datetime | None
    cny_per_kwh: float | None


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    station_id: str
    starts_at: datetime | None
    port_count: int | None
    status: str | None


@dataclass(frozen=True)
class Condition:
    station_id: str
    observed_at: datetime | None
    temperature_c: float | None
    rain: bool | None


@dataclass(frozen=True)
class ChargerStatusEvent:
    """充电桩状态事件；ends_at 为 None 表示状态持续至今。"""

    station_id: str
    port_id: str
    starts_at: datetime | None
    ends_at: datetime | None
    state: str | None


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_stations(raw: list[dict[str, Any]], dq: DataQuality) -> list[Station]:
    stations = []
    for rec in raw:
        sid = rec.get("station_id")
        ports = rec.get("total_ports")
        if not sid:
            dq.add("missing_station_id", "站点记录缺少 station_id", repr(rec)[:80])
            continue
        if not isinstance(ports, int) or ports <= 0:
            dq.add("bad_total_ports", f"站点 {sid} 的 total_ports 缺失或非法", sid)
            continue
        stations.append(Station(sid, rec.get("district"), ports, rec.get("timezone")))
    return stations


def parse_sessions(raw: list[dict[str, Any]], dq: DataQuality) -> list[SessionSegment]:
    segments = []
    for rec in raw:
        sid = rec.get("session_id")
        started = parse_ts(rec.get("started_at"))
        ended = parse_ts(rec.get("ended_at"))
        status = rec.get("status")
        kwh = _num(rec.get("kwh"))
        if not sid or started is None:
            dq.add("unusable_session", "会话缺少 session_id 或 started_at", repr(rec)[:80])
            continue
        if ended is not None and ended < started:
            dq.add("end_before_start", f"会话 {sid} 结束时刻早于开始时刻", sid)
            continue
        if status is not None and status not in SESSION_STATUSES:
            dq.add("unknown_session_status", f"会话 {sid} 状态未知: {status}", sid)
        if kwh is not None and kwh < 0:
            dq.add("negative_kwh", f"会话 {sid} 电量为负", sid)
            kwh = None
        segments.append(
            SessionSegment(sid, rec.get("vehicle_key"), rec.get("station_id"), started, ended, kwh, status)
        )
    return segments


def parse_readings(raw: list[dict[str, Any]], dq: DataQuality) -> list[MeterReading]:
    readings = []
    for rec in raw:
        sid = rec.get("session_id")
        if not sid:
            dq.add("reading_without_session", "电量读数缺少 session_id", repr(rec)[:80])
            continue
        start = _num(rec.get("start_kwh"))
        end = _num(rec.get("end_kwh"))
        if start is not None and end is not None and end < start:
            dq.add("reading_decreased", f"会话 {sid} 结束读数小于开始读数", sid)
        readings.append(MeterReading(sid, start, end))
    return readings


def parse_prices(raw: list[dict[str, Any]], dq: DataQuality) -> list[PriceVersion]:
    versions = []
    for rec in raw:
        vid = rec.get("price_version")
        starts = parse_ts(rec.get("starts_at"))
        ends = parse_ts(rec.get("ends_at"))
        if not vid or starts is None:
            dq.add("unusable_price", "价格版本缺少 price_version 或 starts_at", repr(rec)[:80])
            continue
        if ends is not None and ends <= starts:
            dq.add("bad_price_window", f"价格版本 {vid} 生效窗口非法", vid)
            continue
        versions.append(PriceVersion(vid, starts, ends, _num(rec.get("cny_per_kwh"))))
    return versions


def parse_reservations(raw: list[dict[str, Any]], dq: DataQuality) -> list[Reservation]:
    reservations = []
    for rec in raw:
        rid = rec.get("reservation_id")
        starts = parse_ts(rec.get("starts_at"))
        status = rec.get("status")
        count = rec.get("port_count")
        if not rid or starts is None:
            dq.add("unusable_reservation", "预约缺少 reservation_id 或 starts_at", repr(rec)[:80])
            continue
        if status not in RESERVATION_STATUSES:
            dq.add("unknown_reservation_status", f"预约 {rid} 状态未知: {status}", rid)
        if not isinstance(count, int) or count <= 0:
            dq.add("bad_port_count", f"预约 {rid} 的 port_count 缺失或非法", rid)
            count = None
        reservations.append(Reservation(rid, rec.get("station_id"), starts, count, status))
    return reservations


def parse_conditions(raw: list[dict[str, Any]], dq: DataQuality) -> list[Condition]:
    conditions = []
    for rec in raw:
        observed = parse_ts(rec.get("observed_at"))
        sid = rec.get("station_id")
        if not sid or observed is None:
            dq.add("unusable_condition", "气象记录缺少 station_id 或 observed_at", repr(rec)[:80])
            continue
        rain = rec.get("rain")
        conditions.append(Condition(sid, observed, _num(rec.get("temperature_c")), rain if isinstance(rain, bool) else None))
    return conditions


def parse_charger_status(raw: list[dict[str, Any]], dq: DataQuality) -> list[ChargerStatusEvent]:
    events = []
    for rec in raw:
        sid = rec.get("station_id")
        pid = rec.get("port_id")
        starts = parse_ts(rec.get("starts_at"))
        state = rec.get("state")
        if not sid or not pid or starts is None:
            dq.add("unusable_charger_event", "桩状态事件缺少站点、桩号或开始时刻", repr(rec)[:80])
            continue
        if state not in CHARGER_STATES:
            dq.add("unknown_charger_state", f"桩 {pid} 状态未知: {state}", pid)
        events.append(ChargerStatusEvent(sid, pid, starts, parse_ts(rec.get("ends_at")), state))
    return events
