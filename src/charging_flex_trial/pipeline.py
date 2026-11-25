"""把各分析能力编排成面向调度与管理的报告。

运行一次产出：
- episodes：会话归并结果与不确定性
- price_assignments：每个事件/片段的价格归属
- capacity：分桶容量时间线、每桩真实可用时间
- load_analysis：可比桶、排除原因、价格效果分解、分层负荷、电量统计
- forecast：下一轮目标窗口的占位预测区间
- reservations：容量冲突提示（含受影响预约）、重排建议、承诺有效性
- forecast_vs_actual：目标窗口实际负荷到达后的对照
- conclusions：每条结论的会话范围、价格版本、排除原因与输入指纹
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from . import availability, load_analysis, pricing, reservations, sessions as session_mod
from .audit import Conclusion, compare_forecast_to_actual, fingerprint_bundle, session_scope_dict
from .input_contract import load_bundle
from .models import DataQuality
from .timeutil import bucket_start, iso, iter_buckets


@dataclass(frozen=True)
class Params:
    bucket_minutes: int = 30
    link_gap_minutes: int = 30
    derated_factor: float = availability.DEFAULT_DERATED_FACTOR
    reservation_minutes: int = reservations.DEFAULT_RESERVATION_MINUTES
    condition_max_age_minutes: int = 60
    max_shift_hours: float = 2.0
    forecast_window: tuple[str, str] | None = None  # (start_iso, end_iso)，缺省自动取最新会话日晚高峰

    def as_dict(self) -> dict[str, Any]:
        return {
            "bucket_minutes": self.bucket_minutes,
            "link_gap_minutes": self.link_gap_minutes,
            "derated_factor": self.derated_factor,
            "reservation_minutes": self.reservation_minutes,
            "condition_max_age_minutes": self.condition_max_age_minutes,
            "max_shift_hours": self.max_shift_hours,
        }


def _bundle_window(bundle: dict[str, Any], params: Params) -> tuple[datetime, datetime]:
    moments: list[datetime] = []
    for rec in bundle["sessions.json"]:
        for key in ("started_at", "ended_at"):
            ts = rec.get(key)
            if ts:
                moments.append(datetime.fromisoformat(ts))
    for rec in bundle.get("charger_status.json", []):
        for key in ("starts_at", "ends_at"):
            ts = rec.get(key)
            if ts:
                moments.append(datetime.fromisoformat(ts))
    for rec in bundle["reservations.json"]:
        ts = rec.get("starts_at")
        if ts:
            start = datetime.fromisoformat(ts)
            moments.append(start + timedelta(minutes=params.reservation_minutes))
    for rec in bundle["conditions.json"]:
        ts = rec.get("observed_at")
        if ts:
            moments.append(datetime.fromisoformat(ts))
    if not moments:
        raise ValueError("资料包内没有任何可用时间字段")
    start = bucket_start(min(moments), params.bucket_minutes)
    end_raw = max(moments) + timedelta(minutes=params.reservation_minutes)
    end = bucket_start(end_raw, params.bucket_minutes)
    if end < end_raw:
        end += timedelta(minutes=params.bucket_minutes)
    return start, end


def _default_forecast_window(bundle: dict[str, Any]) -> tuple[datetime, datetime] | None:
    starts = [datetime.fromisoformat(r["started_at"]) for r in bundle["sessions.json"] if r.get("started_at")]
    if not starts:
        return None
    day = max(starts).date()
    return datetime.combine(day, time(16, 0)), datetime.combine(day, time(22, 0))


def build_report(bundle: dict[str, Any], params: Params | None = None) -> dict[str, Any]:
    params = params or Params()
    dq = DataQuality()
    fingerprint = fingerprint_bundle(bundle)
    created_at = datetime.now().isoformat(timespec="seconds")

    from . import models

    station_list = models.parse_stations(bundle["stations.json"], dq)
    segments = models.parse_sessions(bundle["sessions.json"], dq)
    readings = {r.session_id: r for r in models.parse_readings(bundle.get("readings.json", []), dq)}
    price_versions = models.parse_prices(bundle["prices.json"], dq)
    reservation_list = models.parse_reservations(bundle["reservations.json"], dq)
    condition_list = models.parse_conditions(bundle["conditions.json"], dq)
    charger_events = models.parse_charger_status(bundle.get("charger_status.json", []), dq)
    pricing.validate_timeline(price_versions, dq)

    station_ids = [s.station_id for s in station_list]
    known_stations = set(station_ids)
    for seg in segments:
        if seg.station_id not in known_stations:
            dq.add("unknown_station", f"会话 {seg.session_id} 引用未知站点 {seg.station_id}", seg.session_id)
    window_start, window_end = _bundle_window(bundle, params)

    # 1. 会话归并
    episodes = session_mod.build_episodes(
        segments, readings, link_gap=timedelta(minutes=params.link_gap_minutes)
    )

    # 2. 价格归属（已开始会话不被后续调价重算）
    price_assignments = pricing.assign_episode_prices(price_versions, episodes)

    # 3. 容量时间线与每桩真实可用时间
    timeline: list[availability.BucketCapacity] = []
    port_time: dict[str, list[availability.PortAvailability]] = {}
    for st in station_list:
        timeline.extend(
            availability.station_capacity_timeline(
                st, charger_events, window_start, window_end, params.bucket_minutes, dq
            )
        )
        port_time[st.station_id] = availability.port_available_time(
            st, charger_events, window_start, window_end, dq
        )
    capacity = availability.capacity_index(timeline)

    # 4. 负荷与可比桶
    loads = load_analysis.bucket_loads(episodes, station_ids, window_start, window_end, params.bucket_minutes)
    comparable, excluded_buckets = load_analysis.select_comparable_buckets(
        loads,
        capacity,
        condition_list,
        price_versions,
        condition_max_age=timedelta(minutes=params.condition_max_age_minutes),
    )

    # 5. 价格效果分解（相邻价格版本两两比较）
    ordered_versions = sorted(
        {v.price_version: v.starts_at for v in price_versions}.items(), key=lambda kv: kv[1]
    )
    version_names = [name for name, _ in ordered_versions]
    effects = []
    for before, after in zip(version_names, version_names[1:]):
        for sid in station_ids:
            effect = load_analysis.decompose_price_effect(comparable, sid, (before, after))
            effect["hourly_profile"] = {
                period: load_analysis.hourly_profile(comparable, sid, period)
                for period in (before, after)
            }
            effect["vehicle_mix"] = {
                period: load_analysis.vehicle_mix(comparable, sid, period)
                for period in (before, after)
            }
            effects.append(effect)
    energy = load_analysis.energy_stats(episodes, price_assignments)

    # 6. 预测：历史只用目标窗口之前的可比桶，避免泄漏
    if params.forecast_window is not None:
        fw_start = datetime.fromisoformat(params.forecast_window[0])
        fw_end = datetime.fromisoformat(params.forecast_window[1])
    else:
        default_fw = _default_forecast_window(bundle)
        fw_start, fw_end = default_fw if default_fw else (None, None)
    forecast_rows: list[dict[str, Any]] = []
    target_buckets: list[datetime] = []
    if fw_start is not None:
        target_buckets = list(iter_buckets(fw_start, fw_end, params.bucket_minutes))
        history = [c for c in comparable if c.bucket < fw_start]
        for sid in station_ids:
            forecast_rows.extend(load_analysis.forecast_buckets(history, sid, target_buckets))

    # 7. 预约台账、冲突提示与重排
    ledger = reservations.build_ledger(reservation_list, price_versions, capacity, params.bucket_minutes)
    alerts = reservations.capacity_alerts(
        capacity, forecast_rows, ledger, params.reservation_minutes, params.bucket_minutes
    )
    reservations.replan(
        ledger,
        capacity,
        params.bucket_minutes,
        reservation_minutes=params.reservation_minutes,
        max_shift=timedelta(hours=params.max_shift_hours),
    )
    commitments = reservations.commitments_report(ledger)
    replan_rows = [
        {
            "reservation_id": e.reservation.reservation_id,
            "station_id": e.reservation.station_id,
            "status": e.reservation.status,
            "starts_at": iso(e.reservation.starts_at),
            "port_count": e.reservation.port_count,
            "disposition": e.disposition,
            "suggested_start": iso(e.suggested_start),
            "notes": list(e.notes),
        }
        for e in ledger
    ]

    # 8. 预测对照实际（目标窗口内实际到达的负荷）
    forecast_vs_actual = (
        compare_forecast_to_actual(forecast_rows, loads) if forecast_rows else {"buckets": [], "changed_buckets": 0}
    )

    # 9. 结论（可回看：会话范围、价格版本、排除原因、参数、输入指纹）
    included_episodes = sorted(
        {eid for c in comparable for eid in loads[(c.station_id, c.bucket)].episode_ids}
    )
    excluded_episodes = []
    comparable_keys = {(c.station_id, c.bucket) for c in comparable}
    excluded_reason_by_key = {(e["station_id"], e["bucket"]): e["reason"] for e in excluded_buckets}
    for ep in episodes:
        if ep.episode_id in included_episodes:
            continue
        start_bucket = iso(bucket_start(ep.started_at, params.bucket_minutes))
        reason = excluded_reason_by_key.get(
            (ep.segments[0].station_id, start_bucket), "outside_comparable_scope"
        )
        excluded_episodes.append({"episode_id": ep.episode_id, "reason": reason})

    conclusions: list[Conclusion] = []
    for effect in effects:
        sid = effect["station_id"]
        if effect.get("effect") is None:
            summary = f"{sid}：{effect.get('note', '无法给出价格效果')}"
        else:
            eff = effect["effect"]
            summary = (
                f"{sid}：{effect['periods'][0]}→{effect['periods'][1]} 占位变化 "
                f"{eff['raw_change']:+.2f}，其中结构效应 {eff['composition_effect']:+.2f}，"
                f"剔除结构后 {eff['adjusted_change']:+.2f}（{effect['confidence']}）"
            )
        conclusions.append(
            Conclusion(
                kind="price_effect",
                station_scope=[sid],
                summary=summary,
                detail=effect,
                session_scope=session_scope_dict(included_episodes, excluded_episodes),
                price_versions=list(effect["periods"]),
                params=params.as_dict(),
                inputs_fingerprint=fingerprint,
                created_at=created_at,
            )
        )
    for alert in alerts:
        conclusions.append(
            Conclusion(
                kind="capacity_risk",
                station_scope=[alert["station_id"]],
                summary=(
                    f"{alert['station_id']} {alert['bucket']} 需求 {alert['required_ports']} "
                    f"超过可预约容量 {alert['bookable_ports']}，影响 "
                    f"{len(alert['affected_reservations'])} 条预约"
                ),
                detail=alert,
                session_scope=session_scope_dict(included_episodes, excluded_episodes),
                price_versions=version_names,
                params=params.as_dict(),
                inputs_fingerprint=fingerprint,
                created_at=created_at,
            )
        )
    conclusions.append(
        Conclusion(
            kind="commitment",
            station_scope=station_ids,
            summary=f"{sum(1 for c in commitments if c['still_valid'])}/{len(commitments)} 条已承诺预约仍然有效",
            detail={"commitments": commitments, "replan": replan_rows},
            session_scope=session_scope_dict(included_episodes, excluded_episodes),
            price_versions=version_names,
            params=params.as_dict(),
            inputs_fingerprint=fingerprint,
            created_at=created_at,
        )
    )
    conclusions.append(
        Conclusion(
            kind="forecast_change",
            station_scope=station_ids,
            summary=f"目标窗口 {forecast_vs_actual['changed_buckets']} 个桶的实际负荷超出预测区间",
            detail=forecast_vs_actual,
            session_scope=session_scope_dict(included_episodes, excluded_episodes),
            price_versions=version_names,
            params=params.as_dict(),
            inputs_fingerprint=fingerprint,
            created_at=created_at,
        )
    )
    if dq.issues:
        conclusions.append(
            Conclusion(
                kind="data_quality",
                station_scope=station_ids,
                summary=f"{len(dq.issues)} 条资料质量问题",
                detail={"issues": dq.issues},
                session_scope=session_scope_dict(included_episodes, excluded_episodes),
                price_versions=version_names,
                params=params.as_dict(),
                inputs_fingerprint=fingerprint,
                created_at=created_at,
            )
        )

    return {
        "generated_at": created_at,
        "inputs_fingerprint": fingerprint,
        "params": params.as_dict(),
        "window": {"start": iso(window_start), "end": iso(window_end)},
        "forecast_window": {"start": iso(fw_start), "end": iso(fw_end)} if fw_start else None,
        "data_quality": dq.issues,
        "episodes": [
            {
                "episode_id": ep.episode_id,
                "vehicle_key": ep.vehicle_key,
                "session_ids": ep.session_ids,
                "stations": ep.stations,
                "started_at": iso(ep.started_at),
                "ended_at": iso(ep.ended_at),
                "is_closed": ep.is_closed,
                "energy_known_kwh": round(ep.energy_known_kwh, 4),
                "energy_complete": ep.energy_complete,
                "uncertainties": sorted(set(ep.uncertainties)),
                "energies": [
                    {"session_id": e.session_id, "kwh": e.kwh, "source": e.source, "note": e.note}
                    for e in ep.energies
                ],
            }
            for ep in sorted(episodes, key=lambda e: e.started_at)
        ],
        "price_assignments": price_assignments,
        "capacity": {
            "buckets": availability.bucket_summary_rows(timeline),
            "port_available_time": {
                sid: [
                    {
                        "port_id": p.port_id,
                        "available_minutes": p.available_minutes,
                        "derated_minutes": p.derated_minutes,
                        "maintenance_minutes": p.maintenance_minutes,
                        "unknown_minutes": p.unknown_minutes,
                        "usable_minutes": p.usable_minutes,
                    }
                    for p in rows
                ]
                for sid, rows in port_time.items()
            },
        },
        "load_analysis": {
            "comparable_bucket_count": len(comparable),
            "excluded_buckets": excluded_buckets,
            "price_effects": effects,
            "energy": energy,
        },
        "forecast": forecast_rows,
        "reservations": {
            "alerts": alerts,
            "replan": replan_rows,
            "commitments": commitments,
        },
        "forecast_vs_actual": forecast_vs_actual,
        "conclusions": [c.to_dict() for c in conclusions],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 充电价格试验：可归因负荷与容量兑现报告")
    lines.append("")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 输入指纹：{report['inputs_fingerprint']}")
    lines.append(f"- 分析窗口：{report['window']['start']} ~ {report['window']['end']}")
    fw = report.get("forecast_window")
    if fw:
        lines.append(f"- 预测窗口：{fw['start']} ~ {fw['end']}")
    lines.append("")

    lines.append("## 会话归并与电量")
    lines.append("")
    lines.append("| 事件 | 车辆 | 站点 | 开始 | 闭合 | 电量下界(kWh) | 电量完整 | 不确定性 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for ep in report["episodes"]:
        lines.append(
            f"| {ep['episode_id']} | {ep['vehicle_key'] or '（无法关联）'} | {'/'.join(ep['stations'])} "
            f"| {ep['started_at']} | {'是' if ep['is_closed'] else '否'} "
            f"| {ep['energy_known_kwh']} | {'是' if ep['energy_complete'] else '否'} "
            f"| {', '.join(ep['uncertainties']) or '—'} |"
        )
    lines.append("")

    lines.append("## 价格效果归因（可比桶，剔除结构变化）")
    lines.append("")
    for eff in report["load_analysis"]["price_effects"]:
        lines.append(f"### {eff['station_id']}：{' → '.join(eff['periods'])}")
        if eff.get("effect") is None:
            lines.append(f"- 不输出确定的价格效果：{eff.get('note')}")
        else:
            e = eff["effect"]
            lines.append(
                f"- 占位变化 {e['raw_change']:+.2f} = 结构效应 {e['composition_effect']:+.2f} "
                f"+ 层内变化 {e['adjusted_change']:+.2f}（置信度：{eff['confidence']}）"
            )
            lines.append(f"- 说明：{eff['note']}")
            lines.append("| 分层 | 前期样本 | 后期样本 | 前期均值 | 后期均值 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for s in eff.get("strata", []):
                lines.append(
                    f"| {s['stratum']} | {s['n_before']} | {s['n_after']} | {s['mean_before']} | {s['mean_after']} |"
                )
        lines.append("")
    energy = report["load_analysis"]["energy"]
    lines.append("- 电量统计（仅电量完整事件）：")
    for period, stats in energy["by_period"].items():
        lines.append(f"  - {period}：n={stats['n']}，均值 {stats['mean_kwh']} kWh")
    if energy["skipped"]:
        skipped = ", ".join(f"{s['episode_id']}({s['reason']})" for s in energy["skipped"])
        lines.append(f"  - 未计入：{skipped}")
    lines.append("")

    lines.append("## 容量冲突与预约")
    lines.append("")
    alerts = report["reservations"]["alerts"]
    if not alerts:
        lines.append("- 本轮没有发现需求超过可预约容量的时段。")
    for alert in alerts:
        lines.append(
            f"- ⚠ {alert['station_id']} {alert['bucket']}：需求 {alert['required_ports']} > "
            f"可预约 {alert['bookable_ports']}（缺口 {alert['shortfall']}）"
        )
        for aff in alert["affected_reservations"]:
            lines.append(
                f"  - 预约 {aff['reservation_id']}（{aff['status']}，{aff['port_count']} 桩）→ {aff['impact']}"
            )
    lines.append("")
    lines.append("| 预约 | 站点 | 状态 | 开始 | 桩数 | 处置 | 建议起点 | 备注 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in report["reservations"]["replan"]:
        lines.append(
            f"| {row['reservation_id']} | {row['station_id']} | {row['status']} | {row['starts_at']} "
            f"| {row['port_count']} | {row['disposition']} | {row['suggested_start'] or '—'} "
            f"| {'；'.join(row['notes']) or '—'} |"
        )
    lines.append("")
    lines.append("### 已承诺预约的有效性")
    lines.append("")
    for c in report["reservations"]["commitments"]:
        basis = c["basis"]
        lines.append(
            f"- {c['reservation_id']}（{c['status']}，{c['starts_at']}，{c['port_count']} 桩）："
            f"{'仍有效' if c['still_valid'] else '⚠ 容量不足(at_risk)'}；"
            f"依据 价格={basis['price_version']}@{basis['cny_per_kwh']}，"
            f"确认时可预约={basis['bookable_ports_at_start']}"
            f"{'（事后重建）' if basis['reconstructed'] else ''}"
        )
    lines.append("")

    lines.append("## 预测与实际对照")
    lines.append("")
    fva = report["forecast_vs_actual"]
    lines.append(f"- 超出预测区间的桶：{fva['changed_buckets']} 个")
    interesting = [
        row
        for row in fva["buckets"]
        if row["prediction"] is not None or (row["actual_ports_busy"] or 0) > 0
    ]
    skipped = len(fva["buckets"]) - len(interesting)
    lines.append("| 站点 | 桶 | p50 | p90 | 实际 | 结论 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in interesting:
        pred = row["prediction"] or {}
        lines.append(
            f"| {row['station_id']} | {row['bucket']} | {pred.get('p50', '—')} | {pred.get('p90', '—')} "
            f"| {row['actual_ports_busy'] if row['actual_ports_busy'] is not None else '—'} | {row['status']} |"
        )
    if skipped:
        lines.append(f"\n（其余 {skipped} 个桶无历史预测且实际占位为零，从略；完整明细见 report.json）")
    lines.append("")

    lines.append("## 结论回看（会话范围 / 价格版本 / 排除原因）")
    lines.append("")
    lines.append("（会话范围指价格效果分析的可比范围；容量与承诺结论的明细以各自章节为准。）")
    lines.append("")
    for c in report["conclusions"]:
        lines.append(f"- [{c['kind']}] {c['summary']}")
        lines.append(f"  - 价格版本：{', '.join(c['price_versions']) or '—'}；输入指纹：{c['inputs_fingerprint']}")
        excluded = c["session_scope"].get("excluded", [])
        if excluded:
            reason_text = ", ".join(f"{e.get('episode_id', e.get('bucket'))}:{e['reason']}" for e in excluded[:8])
            more = f" 等{len(excluded)}条" if len(excluded) > 8 else ""
            lines.append(f"  - 排除：{reason_text}{more}")
    lines.append("")

    if report["data_quality"]:
        lines.append("## 资料质量")
        lines.append("")
        for issue in report["data_quality"]:
            lines.append(f"- [{issue['code']}] {issue['message']}（{issue['ref']}）")
        lines.append("")
    return "\n".join(lines)


def run(data_dir: str | Path, out_dir: str | Path, params: Params | None = None) -> dict[str, Any]:
    bundle = load_bundle(data_dir)
    report = build_report(bundle, params)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report
