# 资料说明

所有记录均为合成资料（source_kind=synthetic），仅用于本地可复现运行。
缺失、部分质量和待处理状态都是资料含义的一部分：读取层不做静默填充，
分析层不得把缺失当作零或当作正常。

## 通用约定

- 时间戳为站点本地时间（Asia/Shanghai），ISO 分钟级，如 `2026-10-08T18:02`。
- `null` 表示未知/不存在，语义随字段在下文注明。
- 不同文件通过稳定标识（station_id、session_id、vehicle_key、price_version、
  reservation_id、port_id）和时间字段关联。

## source_info.json（必需）

| 字段 | 含义 |
| --- | --- |
| source_kind | 必须为 `synthetic`，否则拒绝加载 |
| schema_version | 资料结构版本 |

## stations.json（必需）

| 字段 | 含义 |
| --- | --- |
| station_id | 站点标识 |
| district | 区域 |
| total_ports | 桩总数；桩号按 `{station_id}-P{1..N}` 推导 |
| timezone | 站点时区 |

## sessions.json（必需）

车辆会话片段。断电会把一次充电拆成多段，车辆也可能跨站充电。

| 字段 | 含义 |
| --- | --- |
| session_id | 片段标识 |
| vehicle_key | 可关联车辆的授权键；`null` 表示无法关联，只能保留不确定性 |
| station_id | 站点 |
| started_at | 开始时刻 |
| ended_at | 结束时刻；`null` 表示未闭合（断电等），不能假定结束时间 |
| kwh | 运营汇总电量；`null` 表示未知，不是零 |
| status | complete / interrupted / active |

归并规则：同一 vehicle_key 且间隔不超过 30 分钟（参数可调）的片段归并为
一个充电事件；跨站在事件上标记 `station_switch`；同车时间重叠不合规，
不合并并标记 `concurrent_segments_same_vehicle`。

## readings.json（可选）

电量表计读数，是电量的优先来源（其次才是 sessions.kwh）。

| 字段 | 含义 |
| --- | --- |
| session_id | 对应会话片段 |
| start_kwh / end_kwh | 起止读数；缺 `end_kwh` 时消耗未知，不能当作零 |

## prices.json（必需）

| 字段 | 含义 |
| --- | --- |
| price_version | 价格版本标识 |
| starts_at / ends_at | 生效窗口，半开区间；`ends_at=null` 表示持续生效 |
| cny_per_kwh | 单价 |

归属规则：按会话开始时刻生效的版本归属；已开始的会话不因随后调价被重算。
窗口重叠时按 starts_at 最新者优先，并记录资料质量告警。

## reservations.json（必需）

| 字段 | 含义 |
| --- | --- |
| reservation_id | 预约标识 |
| station_id / starts_at / port_count | 站点、开始时刻、桩数 |
| status | proposed（未确认，可重排）/ confirmed / completed / cancelled |

预约没有时长字段；分析按显式参数 reservation_minutes（默认 60 分钟）
估计占用区间，该参数会写入每条结论的审计记录。
confirmed/completed 的预约保留当时的价格与容量依据，新状态只能标记
at_risk，不能改写依据。

## conditions.json（必需）

| 字段 | 含义 |
| --- | --- |
| station_id / observed_at | 站点与观测时刻 |
| temperature_c / rain | 温度（℃）与是否降雨；`null` 表示该次观测缺项 |

分桶的气象有效性：桶时刻前后 60 分钟（参数可调）内最近的完整观测；
没有有效观测的桶不参与价格效果比较（排除原因 `no_valid_condition`）。
站点整体没有有效观测时，不输出确定的价格效果。

## charger_status.json（可选）

桩状态事件；`ends_at=null` 表示状态持续至今。

| state | 负荷分析口径 | 可预约容量口径 |
| --- | --- | --- |
| available | 在运 | 可预约 |
| derated（降功率） | 在运，功率按 derated_factor 打折 | 可预约 |
| maintenance（维护） | 不在运 | 不可预约 |
| comms_lost（通讯中断） | 真实状态未知，单列 unknown | 保守不可预约 |

含容量事件（维护/通讯中断）的桶不参与价格效果比较（排除原因
`capacity_event`），但计入运营负荷报告。
