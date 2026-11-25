"""负荷分析与价格效果归因。

核心问题：观察到的负荷差异是价格带来的真实错峰，还是车主、站点、
气象结构改变造成的表面差异。方法：

- 只在“可比桶”上比较：该桶气象观测有效、无维护/通讯中断等容量事件、
  且落在某个价格版本窗口内。被排除的桶和会话必须记录原因。
- 分解：raw_change = composition_effect + adjusted_change，只在两期间
  共同出现的分层（站点×降雨×温度档×峰段）上计算。
  composition_effect 由分层权重变化解释；adjusted_change 是剔除结构
  变化后仍存在的差异，才与价格/行为变化一致（仍非因果证明，结论措辞
  保持 indicative）。
- 站点整体没有有效观测时，不输出确定的价格效果（effect=None）。
- 缺结束读数的会话不计入电量统计（不是零），但仍计入占位负荷。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .availability import BucketCapacity
from .models import Condition
from .pricing import effective_version
from .models import PriceVersion
from .sessions import Episode
from .timeutil import bucket_start, iso, iter_buckets

DEFAULT_CONDITION_MAX_AGE = timedelta(minutes=60)
PEAK_START_HOUR = 17  # 晚高峰 [17, 21)，用于把一天中的时段纳入分层
PEAK_END_HOUR = 21


def daypart(hour: int) -> str:
    return "peak" if PEAK_START_HOUR <= hour < PEAK_END_HOUR else "offpeak"


def temp_bucket(temp_c: float | None) -> str | None:
    if temp_c is None:
        return None
    if temp_c < 10:
        return "cold"
    if temp_c <= 25:
        return "mild"
    return "hot"


def condition_for(
    conditions: list[Condition],
    station_id: str,
    moment: datetime,
    max_age: timedelta = DEFAULT_CONDITION_MAX_AGE,
) -> Condition | None:
    """取站点在 moment 前后 max_age 内最近的一条有效气象观测。"""
    best: Condition | None = None
    best_delta: timedelta | None = None
    for c in conditions:
        if c.station_id != station_id or c.observed_at is None:
            continue
        if c.temperature_c is None and c.rain is None:
            continue
        delta = abs(c.observed_at - moment)
        if delta <= max_age and (best_delta is None or delta < best_delta):
            best, best_delta = c, delta
    return best


@dataclass
class BucketLoad:
    station_id: str
    bucket: datetime
    episode_ids: set[str] = field(default_factory=set)
    vehicle_keys: set[str] = field(default_factory=set)
    unlinked_episodes: int = 0
    open_segment_caveat: bool = False  # 有未闭合片段，后续桶负荷可能被低估

    @property
    def ports_busy(self) -> int:
        return len(self.episode_ids)


def bucket_loads(
    episodes: list[Episode],
    station_ids: list[str],
    window_start: datetime,
    window_end: datetime,
    bucket_minutes: int,
) -> dict[tuple[str, datetime], BucketLoad]:
    """每个站点每个分桶的占位负荷（同一事件在同一桶只计一次）。

    未闭合片段只计入其开始桶，并给该站后续桶打上可能被低估的标记，
    而不是假设它的结束时刻。
    """
    loads: dict[tuple[str, datetime], BucketLoad] = {}
    step = timedelta(minutes=bucket_minutes)
    for sid in station_ids:
        for b in iter_buckets(window_start, window_end, bucket_minutes):
            loads[(sid, b)] = BucketLoad(sid, b)

    open_segments_by_station: dict[str, list[datetime]] = {}
    for ep in episodes:
        for seg in ep.segments:
            b = bucket_start(seg.started_at, bucket_minutes)  # type: ignore[arg-type]
            if seg.ended_at is None:
                open_segments_by_station.setdefault(seg.station_id, []).append(seg.started_at)  # type: ignore[arg-type]
            while True:
                load = loads.get((seg.station_id, b))
                if load is not None:
                    load.episode_ids.add(ep.episode_id)
                    if ep.vehicle_key:
                        load.vehicle_keys.add(ep.vehicle_key)
                    else:
                        load.unlinked_episodes += 1
                b += step
                # 半开区间 [start, end)：结束时刻所在桶不再计入；
                # 未闭合片段只计入开始桶，不假设其结束时刻。
                if seg.ended_at is None or b >= seg.ended_at:
                    break

    for (sid, b), load in loads.items():
        for open_start in open_segments_by_station.get(sid, []):
            if open_start < b:
                load.open_segment_caveat = True
    return loads


@dataclass
class ComparableBucket:
    station_id: str
    bucket: datetime
    period: str  # price_version
    stratum: tuple[str, str, str, str]  # (station, rain, temp_bucket, daypart)
    ports_busy: int
    unique_vehicles: int
    unlinked_episodes: int


def select_comparable_buckets(
    loads: dict[tuple[str, datetime], BucketLoad],
    capacity: dict[tuple[str, datetime], BucketCapacity],
    conditions: list[Condition],
    price_versions: list[PriceVersion],
    condition_max_age: timedelta = DEFAULT_CONDITION_MAX_AGE,
) -> tuple[list[ComparableBucket], list[dict[str, str]]]:
    """挑出可比桶；被排除的桶逐条记录原因。"""
    comparable: list[ComparableBucket] = []
    excluded: list[dict[str, str]] = []
    for (sid, b), load in sorted(loads.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        cap = capacity.get((sid, b))
        if cap is not None and (cap.maintenance > 0 or cap.comms_lost > 0):
            excluded.append({"station_id": sid, "bucket": iso(b), "reason": "capacity_event"})
            continue
        cond = condition_for(conditions, sid, b, condition_max_age)
        rain = cond.rain if cond else None
        tb = temp_bucket(cond.temperature_c if cond else None)
        if rain is None or tb is None:
            excluded.append({"station_id": sid, "bucket": iso(b), "reason": "no_valid_condition"})
            continue
        version = effective_version(price_versions, b)
        if version is None:
            excluded.append({"station_id": sid, "bucket": iso(b), "reason": "no_price_version"})
            continue
        comparable.append(
            ComparableBucket(
                station_id=sid,
                bucket=b,
                period=version.price_version,
                stratum=(sid, str(rain), tb, daypart(b.hour)),
                ports_busy=load.ports_busy,
                unique_vehicles=len(load.vehicle_keys),
                unlinked_episodes=load.unlinked_episodes,
            )
        )
    return comparable, excluded


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def decompose_price_effect(
    comparable: list[ComparableBucket],
    station_id: str,
    periods: tuple[str, str],
) -> dict[str, object]:
    """把两价格期间的平均占位差异分解为结构效应与层内变化。"""
    before, after = periods
    rows = [c for c in comparable if c.station_id == station_id and c.period in periods]
    by_stratum: dict[tuple[str, str, str], dict[str, list[int]]] = {}
    for c in rows:
        slot = by_stratum.setdefault(c.stratum, {before: [], after: []})
        slot[c.period].append(c.ports_busy)

    shared = {s: v for s, v in by_stratum.items() if v[before] and v[after]}
    only_before = [s for s, v in by_stratum.items() if v[before] and not v[after]]
    only_after = [s for s, v in by_stratum.items() if v[after] and not v[before]]

    if not rows:
        return {
            "station_id": station_id,
            "periods": list(periods),
            "effect": None,
            "reason": "no_valid_observations",
            "note": "站点整体没有有效观测，不输出确定的价格效果",
        }
    if not shared:
        return {
            "station_id": station_id,
            "periods": list(periods),
            "effect": None,
            "reason": "no_shared_strata",
            "note": "两期间没有共同分层，无法区分结构变化与价格效果",
            "strata_only_before": [list(s) for s in only_before],
            "strata_only_after": [list(s) for s in only_after],
        }

    n0 = sum(len(v[before]) for v in shared.values())
    n1 = sum(len(v[after]) for v in shared.values())
    m0 = {s: _mean(v[before]) for s, v in shared.items()}
    m1 = {s: _mean(v[after]) for s, v in shared.items()}
    w0 = {s: len(v[before]) / n0 for s, v in shared.items()}
    w1 = {s: len(v[after]) / n1 for s, v in shared.items()}

    raw_before = sum(w0[s] * m0[s] for s in shared)
    raw_after = sum(w1[s] * m1[s] for s in shared)
    composition_effect = sum((w1[s] - w0[s]) * m1[s] for s in shared)
    adjusted_change = sum(w0[s] * (m1[s] - m0[s]) for s in shared)

    insufficient = any(len(v[before]) < 2 or len(v[after]) < 2 for v in shared.values())
    return {
        "station_id": station_id,
        "periods": [before, after],
        "effect": {
            "raw_change": round(raw_after - raw_before, 4),
            "composition_effect": round(composition_effect, 4),
            "adjusted_change": round(adjusted_change, 4),
            "raw_before": round(raw_before, 4),
            "raw_after": round(raw_after, 4),
        },
        "confidence": "insufficient_data" if insufficient else "indicative",
        "note": "adjusted_change 与价格/行为变化一致，但不是因果证明"
        + ("；分层样本过少，仅供参考" if insufficient else ""),
        "strata": [
            {
                "stratum": list(s),
                "n_before": len(shared[s][before]),
                "n_after": len(shared[s][after]),
                "mean_before": round(m0[s], 4),
                "mean_after": round(m1[s], 4),
            }
            for s in sorted(shared)
        ],
        "strata_only_before": [list(s) for s in only_before],
        "strata_only_after": [list(s) for s in only_after],
    }


def hourly_profile(
    comparable: list[ComparableBucket], station_id: str, period: str
) -> dict[int, float]:
    """站点在某价格期间内，各小时（0-23）的平均占位，用于观察峰移。"""
    by_hour: dict[int, list[int]] = {}
    for c in comparable:
        if c.station_id == station_id and c.period == period:
            by_hour.setdefault(c.bucket.hour, []).append(c.ports_busy)
    return {h: round(_mean(v), 4) for h, v in sorted(by_hour.items())}


def vehicle_mix(comparable: list[ComparableBucket], station_id: str, period: str) -> dict[str, object]:
    """车主结构描述：可关联车辆与无法关联样本的占比（保留不确定性）。"""
    rows = [c for c in comparable if c.station_id == station_id and c.period == period]
    vehicles = sum(c.unique_vehicles for c in rows)
    unlinked = sum(c.unlinked_episodes for c in rows)
    return {
        "linked_vehicle_observations": vehicles,
        "unlinked_episode_observations": unlinked,
        "unlinked_share": round(unlinked / (vehicles + unlinked), 4) if vehicles + unlinked else None,
    }


def energy_stats(episodes: list[Episode], price_by_episode: dict[str, dict[str, object]]) -> dict[str, object]:
    """按价格期间统计单次充电电量；电量不完整的事件不计入（不是零）。"""
    by_period: dict[str, list[float]] = {}
    skipped: list[dict[str, str]] = []
    for ep in episodes:
        assignment = price_by_episode.get(ep.episode_id)
        period = assignment.get("price_version") if assignment else None
        if not period:
            skipped.append({"episode_id": ep.episode_id, "reason": "no_price_version"})
            continue
        if not ep.energy_complete:
            skipped.append({"episode_id": ep.episode_id, "reason": "energy_incomplete"})
            continue
        by_period.setdefault(str(period), []).append(ep.energy_known_kwh)
    return {
        "by_period": {
            p: {"n": len(v), "mean_kwh": round(_mean(v), 4)} for p, v in sorted(by_period.items())
        },
        "skipped": skipped,
    }


def forecast_buckets(
    comparable: list[ComparableBucket],
    station_id: str,
    target_buckets: list[datetime],
) -> list[dict[str, object]]:
    """按站点+小时用历史可比桶给出占位预测区间（p50/p90，最近秩）。"""
    history: dict[int, list[int]] = {}
    for c in comparable:
        if c.station_id == station_id:
            history.setdefault(c.bucket.hour, []).append(c.ports_busy)

    def percentile(values: list[int], q: float) -> float:
        ordered = sorted(values)
        rank = max(1, round(q * len(ordered)))
        return float(ordered[min(rank, len(ordered)) - 1])

    rows: list[dict[str, object]] = []
    for b in target_buckets:
        values = history.get(b.hour, [])
        if not values:
            rows.append(
                {"station_id": station_id, "bucket": iso(b), "prediction": None, "reason": "no_history"}
            )
            continue
        rows.append(
            {
                "station_id": station_id,
                "bucket": iso(b),
                "prediction": {
                    "p50": percentile(values, 0.5),
                    "p90": percentile(values, 0.9),
                    "n_history": len(values),
                },
                "low_confidence": len(values) < 3,
            }
        )
    return rows
