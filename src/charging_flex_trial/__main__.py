"""命令行入口：python3 -m charging_flex_trial <data_dir> <out_dir>"""

from __future__ import annotations

import sys

from .pipeline import run


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法: python3 -m charging_flex_trial <data_dir> <out_dir>", file=sys.stderr)
        return 2
    report = run(argv[1], argv[2])
    print(f"报告已生成：{argv[2]}/report.json 与 report.md（结论 {len(report['conclusions'])} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
