"""审计与回看。

每个结论都记录：会话/桶范围、价格版本、排除原因、参数与输入指纹，
管理者可以据此回看待定结论的依据。预测与实际负荷的比较也在这里
定义，下一轮实际负荷到达后可以看到预测改变了什么、哪些承诺仍有效。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .timeutil import iso


def fingerprint_bundle(bundle: dict[str, Any]) -> str:
    """输入资料包的内容指纹，用于把结论绑定到具体输入版本。"""
    canonical = json.dumps(bundle, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class Conclusion:
    """一条可回看的结论。"""

    kind: str  # price_effect / capacity_risk / commitment / forecast_change / data_quality
    station_scope: list[str]
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    session_scope: dict[str, Any] = field(default_factory=dict)  # included / excluded(含原因)
    price_versions: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    inputs_fingerprint: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "station_scope": self.station_scope,
            "summary": self.summary,
            "detail": self.detail,
            "session_scope": self.session_scope,
            "price_versions": self.price_versions,
            "params": self.params,
            "inputs_fingerprint": self.inputs_fingerprint,
            "created_at": self.created_at,
        }


def compare_forecast_to_actual(
    forecast_rows: list[dict[str, Any]],
    actual_loads: dict[tuple[str, datetime], Any],
) -> dict[str, Any]:
    """下一轮实际负荷到达后，对照预测区间给出逐桶结论。"""
    rows = []
    changed = 0
    for row in forecast_rows:
        prediction = row.get("prediction")
        key = (row["station_id"], datetime.fromisoformat(row["bucket"]))  # type: ignore[arg-type]
        load = actual_loads.get(key)
        actual = load.ports_busy if load is not None else None
        if prediction is None:
            status = "no_prediction"
        elif actual is None:
            status = "no_actual"
        elif actual > prediction["p90"]:  # type: ignore[index]
            status = "above_range"
            changed += 1
        elif actual < prediction["p50"]:  # type: ignore[index]
            status = "below_range"
            changed += 1
        else:
            status = "within_range"
        rows.append(
            {
                "station_id": row["station_id"],
                "bucket": row["bucket"],
                "prediction": prediction,
                "actual_ports_busy": actual,
                "status": status,
            }
        )
    return {
        "buckets": rows,
        "changed_buckets": changed,
        "note": "above/below_range 表示实际负荷超出预测区间，需要据此调整下一轮试验与容量安排",
    }


def session_scope_dict(included: list[str], excluded: list[dict[str, str]]) -> dict[str, Any]:
    return {"included": included, "excluded": excluded}
