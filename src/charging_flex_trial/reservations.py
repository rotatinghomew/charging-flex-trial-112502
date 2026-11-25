"""预约台账、容量冲突提示与重排。

规则：
- 预约时长不可知，采用显式参数 reservation_minutes（会写入审计记录）。
- 已确认/已完成的预约保留当时的价格与容量依据（basis），新状态到达
  只能把它标记为 at_risk，不能改写依据。
- 未确认（proposed）的预约可以在新状态到达后重新安排。
- 需求可能超过容量的桶，必须列出会影响哪些预约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .availability import BucketCapacity
from .models import PriceVersion, Reservation
from .pricing import effective_version
from .timeutil import bucket_start, iso

DEFAULT_RESERVATION_MINUTES = 60
DEFAULT_MAX_SHIFT = timedelta(hours=2)


@dataclass
class ReservationBasis:
    """确认/完成时刻锁定的依据。"""

    price_version: str | None
    cny_per_kwh: float | None
    bookable_ports_at_start: int | None
    reconstructed: bool  # True 表示依据是事后重建而非确认当时记录


@dataclass
class LedgerEntry:
    reservation: Reservation
    basis: ReservationBasis | None = None  # 仅 confirmed/completed 持有
    disposition: str = "pending"  # confirmed / completed / confirmable / suggest_move / unfulfillable / at_risk / cancelled
    suggested_start: datetime | None = None
    notes: list[str] = field(default_factory=list)


def _occupied_buckets(starts_at: datetime, reservation_minutes: int, bucket_minutes: int) -> list[datetime]:
    first = bucket_start(starts_at, bucket_minutes)
    end = starts_at + timedelta(minutes=reservation_minutes)
    buckets = []
    b = first
    while b < end:
        buckets.append(b)
        b += timedelta(minutes=bucket_minutes)
    return buckets


def build_ledger(
    reservations: list[Reservation],
    price_versions: list[PriceVersion],
    capacity: dict[tuple[str, datetime], BucketCapacity],
    bucket_minutes: int,
) -> list[LedgerEntry]:
    """把原始预约装入台账；已确认/完成的重建当时依据并标记 reconstructed。"""
    ledger: list[LedgerEntry] = []
    for res in reservations:
        entry = LedgerEntry(reservation=res)
        if res.status in ("confirmed", "completed"):
            version = effective_version(price_versions, res.starts_at)  # type: ignore[arg-type]
            start_bucket = bucket_start(res.starts_at, bucket_minutes)  # type: ignore[arg-type]
            cap = capacity.get((res.station_id, start_bucket))
            entry.basis = ReservationBasis(
                price_version=version.price_version if version else None,
                cny_per_kwh=version.cny_per_kwh if version else None,
                bookable_ports_at_start=cap.bookable if cap else None,
                reconstructed=True,
            )
            entry.disposition = res.status
        elif res.status == "cancelled":
            entry.disposition = "cancelled"
        ledger.append(entry)
    return ledger


def reserved_ports_by_bucket(
    entries: list[LedgerEntry],
    statuses: tuple[str, ...],
    reservation_minutes: int,
    bucket_minutes: int,
) -> dict[tuple[str, datetime], int]:
    """指定状态的预约在各桶占用的桩数。"""
    usage: dict[tuple[str, datetime], int] = {}
    for entry in entries:
        res = entry.reservation
        if res.status not in statuses or res.port_count is None or res.starts_at is None:
            continue
        for b in _occupied_buckets(res.starts_at, reservation_minutes, bucket_minutes):
            key = (res.station_id, b)
            usage[key] = usage.get(key, 0) + res.port_count
    return usage


def capacity_alerts(
    capacity: dict[tuple[str, datetime], BucketCapacity],
    forecast_rows: list[dict[str, object]],
    entries: list[LedgerEntry],
    reservation_minutes: int,
    bucket_minutes: int,
) -> list[dict[str, object]]:
    """需求可能超过容量的桶，连同受影响的预约一起列出。

    需求取预测 p90 与已承诺（confirmed+completed）预约占用的较大者。
    """
    committed = reserved_ports_by_bucket(entries, ("confirmed", "completed"), reservation_minutes, bucket_minutes)
    demand: dict[tuple[str, datetime], float] = {}
    for row in forecast_rows:
        prediction = row.get("prediction")
        if not prediction:
            continue
        key = (row["station_id"], datetime.fromisoformat(row["bucket"]))  # type: ignore[arg-type]
        demand[key] = max(demand.get(key, 0.0), float(prediction["p90"]))  # type: ignore[index]
    for key, ports in committed.items():
        demand[key] = max(demand.get(key, 0.0), float(ports))

    alerts: list[dict[str, object]] = []
    for (sid, b), required in sorted(demand.items()):
        cap = capacity.get((sid, b))
        if cap is None or required <= cap.bookable:
            continue
        affected = []
        for entry in entries:
            res = entry.reservation
            if res.station_id != sid or res.status in (None, "cancelled") or res.starts_at is None:
                continue
            if b in _occupied_buckets(res.starts_at, reservation_minutes, bucket_minutes):
                affected.append(
                    {
                        "reservation_id": res.reservation_id,
                        "status": res.status,
                        "port_count": res.port_count,
                        "impact": "commitment_at_risk" if res.status in ("confirmed", "completed") else "reschedule_candidate",
                    }
                )
        alerts.append(
            {
                "station_id": sid,
                "bucket": iso(b),
                "required_ports": required,
                "bookable_ports": cap.bookable,
                "shortfall": required - cap.bookable,
                "affected_reservations": affected,
            }
        )
    return alerts


def replan(
    entries: list[LedgerEntry],
    capacity: dict[tuple[str, datetime], BucketCapacity],
    bucket_minutes: int,
    reservation_minutes: int = DEFAULT_RESERVATION_MINUTES,
    max_shift: timedelta = DEFAULT_MAX_SHIFT,
) -> list[LedgerEntry]:
    """按最新容量重排：proposed 可确认/移动/无法满足；confirmed/completed 只校验不改依据。"""
    committed = reserved_ports_by_bucket(entries, ("confirmed", "completed"), reservation_minutes, bucket_minutes)
    # proposed 之间也按处理顺序互相占用，避免把两个 proposed 排进同一空位
    planned: dict[tuple[str, datetime], int] = dict(committed)

    def fits(station_id: str, start: datetime, port_count: int) -> bool:
        for b in _occupied_buckets(start, reservation_minutes, bucket_minutes):
            cap = capacity.get((station_id, b))
            if cap is None:
                return False
            if planned.get((station_id, b), 0) + port_count > cap.bookable:
                return False
        return True

    def reserve(station_id: str, start: datetime, port_count: int) -> None:
        for b in _occupied_buckets(start, reservation_minutes, bucket_minutes):
            key = (station_id, b)
            planned[key] = planned.get(key, 0) + port_count

    step = timedelta(minutes=bucket_minutes)
    for entry in entries:
        res = entry.reservation
        if res.status in ("confirmed", "completed"):
            # 已承诺需求是否在新容量状态下超限；超限只标记 at_risk，不改依据
            over = False
            if res.port_count is not None and res.starts_at is not None:
                for b in _occupied_buckets(res.starts_at, reservation_minutes, bucket_minutes):
                    cap = capacity.get((res.station_id, b))
                    if cap is not None and planned.get((res.station_id, b), 0) > cap.bookable:
                        over = True
            if over:
                entry.disposition = "at_risk"
                entry.notes.append("新容量状态下已承诺需求超过可预约容量；保留原价格与容量依据")
            continue
        if res.status != "proposed" or res.port_count is None or res.starts_at is None:
            continue
        if fits(res.station_id, res.starts_at, res.port_count):
            entry.disposition = "confirmable"
            reserve(res.station_id, res.starts_at, res.port_count)
            continue
        # 在 ±max_shift 内找最近的可行起点
        best: datetime | None = None
        offset = step
        while offset <= max_shift:
            for candidate in (res.starts_at + offset, res.starts_at - offset):
                if fits(res.station_id, candidate, res.port_count):
                    best = candidate
                    break
            if best is not None:
                break
            offset += step
        if best is not None:
            entry.disposition = "suggest_move"
            entry.suggested_start = best
            entry.notes.append(f"原起点容量不足，建议移至 {iso(best)}")
            reserve(res.station_id, best, res.port_count)
        else:
            entry.disposition = "unfulfillable"
            entry.notes.append("在允许的调整窗口内没有可预约容量")
    return entries


def commitments_report(entries: list[LedgerEntry]) -> list[dict[str, object]]:
    """已确认/已完成承诺及其当前有效性。"""
    rows = []
    for entry in entries:
        res = entry.reservation
        if res.status not in ("confirmed", "completed"):
            continue
        rows.append(
            {
                "reservation_id": res.reservation_id,
                "station_id": res.station_id,
                "starts_at": iso(res.starts_at),
                "port_count": res.port_count,
                "status": res.status,
                "still_valid": entry.disposition != "at_risk",
                "basis": {
                    "price_version": entry.basis.price_version if entry.basis else None,
                    "cny_per_kwh": entry.basis.cny_per_kwh if entry.basis else None,
                    "bookable_ports_at_start": entry.basis.bookable_ports_at_start if entry.basis else None,
                    "reconstructed": entry.basis.reconstructed if entry.basis else None,
                },
                "notes": list(entry.notes),
            }
        )
    return rows
