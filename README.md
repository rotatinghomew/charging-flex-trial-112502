# 充电价格试验的可归因负荷与容量兑现

这是一个保存合成业务资料的 Python 项目。资料加载层只负责读取原始记录和
检查输入契约；业务分析、解释和资源安排能力在 `src/charging_flex_trial/`
中实现：

- `sessions.py`：会话归并。同一车辆被断电拆段、跨站切换的片段归并为一个
  充电事件；无法关联的车辆保留不确定性；缺结束读数的电量是未知下界，不是零。
- `pricing.py`：价格版本按生效时刻归属于新会话；已开始的会话不因随后调价
  被重算。
- `availability.py`：维护/降功率/通讯中断对负荷分析与可预约容量的不同含义；
  每桩真实可用时间。
- `load_analysis.py`：可比桶选择（排除原因逐条记录）、站点×降雨×温度档×峰段
  分层、占位变化分解为结构效应与层内变化、分桶负荷预测区间。站点整体没有
  有效观测时不输出确定的价格效果。
- `reservations.py`：预约台账；需求可能超过容量时列出受影响预约；新状态到达
  后 proposed 可重排，confirmed/completed 保留当时价格与容量依据。
- `audit.py`：每条结论记录会话范围、价格版本、排除原因、参数与输入指纹；
  预测与实际负荷对照。
- `pipeline.py`：编排以上能力，产出调度/管理报告。

资料位于 data/，所有记录都带有合成资料标记。不同文件之间通过稳定标识和
时间字段关联，缺失、部分质量和待处理状态都是资料含义的一部分，不能在读取
时静默填充。字段说明见 docs/data-dictionary.md。

运行基线测试：python3 -m unittest discover -s tests -v
检查 Python 文件：python3 -m compileall src tests
生成分析报告：PYTHONPATH=src python3 -m charging_flex_trial data out
（产出 out/report.json 与 out/report.md）
