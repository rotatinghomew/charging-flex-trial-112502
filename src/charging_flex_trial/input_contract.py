"""读取合成资料并执行最小输入契约校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED_FILES = ('source_info.json', 'stations.json', 'sessions.json', 'prices.json', 'reservations.json', 'conditions.json')

# 业务分析需要的补充输入；缺失时对应能力降级而不是凭空假设。
OPTIONAL_FILES = ('charger_status.json', 'readings.json')


def load_bundle(root: str | Path) -> dict[str, Any]:
    """读取资料包，保留原始记录、缺失值和顺序。"""
    base = Path(root)
    bundle: dict[str, Any] = {}
    for name in REQUIRED_FILES:
        path = base / name
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open(encoding="utf-8") as handle:
            bundle[name] = json.load(handle)
    for name in OPTIONAL_FILES:
        path = base / name
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                bundle[name] = json.load(handle)
    if bundle["source_info.json"].get("source_kind") != "synthetic":
        raise ValueError("资料包必须标明为 synthetic")
    return bundle
