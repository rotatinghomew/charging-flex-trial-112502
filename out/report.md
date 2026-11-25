# 充电价格试验：可归因负荷与容量兑现报告

- 生成时间：2026-11-25T02:50:13
- 输入指纹：10306a45aa41a7cf
- 分析窗口：2026-10-08T18:00 ~ 2026-10-16T21:00
- 预测窗口：2026-10-16T16:00 ~ 2026-10-16T22:00

## 会话归并与电量

| 事件 | 车辆 | 站点 | 开始 | 闭合 | 电量下界(kWh) | 电量完整 | 不确定性 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ep-ch-001a | v-7 | S-01 | 2026-10-08T18:02 | 否 | 21.4 | 否 | energy_incomplete, open_segment |
| ep-ch-006 | （无法关联） | S-03 | 2026-10-08T18:15 | 是 | 0 | 否 | energy_incomplete, vehicle_not_linkable |
| ep-ch-005 | v-3 | S-01 | 2026-10-08T18:20 | 是 | 24.0 | 是 | — |
| ep-ch-002 | （无法关联） | S-02 | 2026-10-08T19:10 | 是 | 18.0 | 是 | vehicle_not_linkable |
| ep-ch-003a | v-9 | S-01/S-02 | 2026-10-15T12:00 | 是 | 32.5 | 是 | station_switch |
| ep-ch-007 | v-3 | S-01 | 2026-10-16T18:15 | 是 | 22.6 | 是 | — |
| ep-ch-008 | （无法关联） | S-01 | 2026-10-16T18:40 | 是 | 12.0 | 是 | vehicle_not_linkable |
| ep-ch-009 | v-4 | S-02 | 2026-10-16T19:05 | 是 | 16.2 | 是 | — |

## 价格效果归因（可比桶，剔除结构变化）

### S-01：p-early → p-flex
- 占位变化 -2.00 = 结构效应 +0.00 + 层内变化 -2.00（置信度：insufficient_data）
- 说明：adjusted_change 与价格/行为变化一致，但不是因果证明；分层样本过少，仅供参考
| 分层 | 前期样本 | 后期样本 | 前期均值 | 后期均值 |
| --- | --- | --- | --- | --- |
| ['S-01', 'False', 'mild', 'peak'] | 1 | 1 | 2.0 | 0.0 |

### S-02：p-early → p-flex
- 占位变化 +0.15 = 结构效应 +0.00 + 层内变化 +0.15（置信度：indicative）
- 说明：adjusted_change 与价格/行为变化一致，但不是因果证明
| 分层 | 前期样本 | 后期样本 | 前期均值 | 后期均值 |
| --- | --- | --- | --- | --- |
| ['S-02', 'True', 'mild', 'peak'] | 4 | 5 | 0.25 | 0.4 |

### S-03：p-early → p-flex
- 不输出确定的价格效果：站点整体没有有效观测，不输出确定的价格效果

- 电量统计（仅电量完整事件）：
  - p-early：n=2，均值 21.0 kWh
  - p-flex：n=4，均值 20.825 kWh
  - 未计入：ep-ch-006(energy_incomplete), ep-ch-001a(energy_incomplete)

## 容量冲突与预约

- ⚠ S-01 2026-10-16T18:00：需求 2.0 > 可预约 1（缺口 1.0）
  - 预约 r-003（proposed，3 桩）→ reschedule_candidate
- ⚠ S-01 2026-10-16T18:30：需求 2.0 > 可预约 1（缺口 1.0）
  - 预约 r-003（proposed，3 桩）→ reschedule_candidate
  - 预约 r-004（confirmed，2 桩）→ commitment_at_risk

| 预约 | 站点 | 状态 | 开始 | 桩数 | 处置 | 建议起点 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| r-001 | S-01 | confirmed | 2026-10-08T19:00 | 2 | confirmed | — | — |
| r-002 | S-01 | proposed | 2026-10-08T19:00 | 3 | suggest_move | 2026-10-08T20:00 | 原起点容量不足，建议移至 2026-10-08T20:00 |
| r-000 | S-01 | completed | 2026-10-08T18:00 | 1 | completed | — | — |
| r-003 | S-01 | proposed | 2026-10-16T18:00 | 3 | suggest_move | 2026-10-16T19:30 | 原起点容量不足，建议移至 2026-10-16T19:30 |
| r-004 | S-01 | confirmed | 2026-10-16T18:30 | 2 | at_risk | — | 新容量状态下已承诺需求超过可预约容量；保留原价格与容量依据 |
| r-005 | S-02 | proposed | 2026-10-16T19:00 | 2 | confirmable | — | — |

### 已承诺预约的有效性

- r-001（confirmed，2026-10-08T19:00，2 桩）：仍有效；依据 价格=p-early@1.2，确认时可预约=3（事后重建）
- r-000（completed，2026-10-08T18:00，1 桩）：仍有效；依据 价格=p-early@1.2，确认时可预约=4（事后重建）
- r-004（confirmed，2026-10-16T18:30，2 桩）：⚠ 容量不足(at_risk)；依据 价格=p-flex@0.8，确认时可预约=1（事后重建）

## 预测与实际对照

- 超出预测区间的桶：1 个
| 站点 | 桶 | p50 | p90 | 实际 | 结论 |
| --- | --- | --- | --- | --- | --- |
| S-01 | 2026-10-16T18:00 | 2.0 | 2.0 | 1 | below_range |
| S-01 | 2026-10-16T18:30 | 2.0 | 2.0 | 2 | within_range |
| S-01 | 2026-10-16T19:00 | — | — | 1 | no_prediction |
| S-02 | 2026-10-16T18:00 | 0.0 | 0.0 | 0 | within_range |
| S-02 | 2026-10-16T18:30 | 0.0 | 0.0 | 0 | within_range |
| S-02 | 2026-10-16T19:00 | 1.0 | 1.0 | 1 | within_range |
| S-02 | 2026-10-16T19:30 | 1.0 | 1.0 | 1 | within_range |
| S-02 | 2026-10-16T20:00 | 0.0 | 0.0 | 0 | within_range |
| S-02 | 2026-10-16T20:30 | 0.0 | 0.0 | 0 | within_range |

（其余 27 个桶无历史预测且实际占位为零，从略；完整明细见 report.json）

## 结论回看（会话范围 / 价格版本 / 排除原因）

（会话范围指价格效果分析的可比范围；容量与承诺结论的明细以各自章节为准。）

- [price_effect] S-01：p-early→p-flex 占位变化 -2.00，其中结构效应 +0.00，剔除结构后 -2.00（insufficient_data）
  - 价格版本：p-early, p-flex；输入指纹：10306a45aa41a7cf
  - 排除：ep-ch-006:capacity_event, ep-ch-008:capacity_event, ep-ch-007:capacity_event
- [price_effect] S-02：p-early→p-flex 占位变化 +0.15，其中结构效应 +0.00，剔除结构后 +0.15（indicative）
  - 价格版本：p-early, p-flex；输入指纹：10306a45aa41a7cf
  - 排除：ep-ch-006:capacity_event, ep-ch-008:capacity_event, ep-ch-007:capacity_event
- [price_effect] S-03：站点整体没有有效观测，不输出确定的价格效果
  - 价格版本：p-early, p-flex；输入指纹：10306a45aa41a7cf
  - 排除：ep-ch-006:capacity_event, ep-ch-008:capacity_event, ep-ch-007:capacity_event
- [capacity_risk] S-01 2026-10-16T18:00 需求 2.0 超过可预约容量 1，影响 1 条预约
  - 价格版本：p-early, p-flex；输入指纹：10306a45aa41a7cf
  - 排除：ep-ch-006:capacity_event, ep-ch-008:capacity_event, ep-ch-007:capacity_event
- [capacity_risk] S-01 2026-10-16T18:30 需求 2.0 超过可预约容量 1，影响 2 条预约
  - 价格版本：p-early, p-flex；输入指纹：10306a45aa41a7cf
  - 排除：ep-ch-006:capacity_event, ep-ch-008:capacity_event, ep-ch-007:capacity_event
- [commitment] 2/3 条已承诺预约仍然有效
  - 价格版本：p-early, p-flex；输入指纹：10306a45aa41a7cf
  - 排除：ep-ch-006:capacity_event, ep-ch-008:capacity_event, ep-ch-007:capacity_event
- [forecast_change] 目标窗口 1 个桶的实际负荷超出预测区间
  - 价格版本：p-early, p-flex；输入指纹：10306a45aa41a7cf
  - 排除：ep-ch-006:capacity_event, ep-ch-008:capacity_event, ep-ch-007:capacity_event
