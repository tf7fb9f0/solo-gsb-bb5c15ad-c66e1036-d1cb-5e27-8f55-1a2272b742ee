# DNS SOA 序列号推进审计 API

基于 **Python + FastAPI**，按 [RFC 1982](https://www.rfc-editor.org/rfc/rfc1982) 序列号环形算术，
对多个 DNS 副本当前持有的 32 位无符号 SOA 序列号与拟发布值进行推进审计，
推导最优发布候选，规划各副本的 IXFR / AXFR 传输，
在字节预算内选择要保留的增量集合（支持按故障域公平分配），
并按时间序列观测对上线做 quorum 门禁判定、
评估每次发布在各故障域间的传播收敛时延（SLO）。

## 一条命令启动（Docker Compose）

```bash
docker compose up --build -d
```

服务地址（端口 **8000**）：

- 健康检查：<http://localhost:8000/health>
- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>

停止服务：

```bash
docker compose down
```

> 无 Docker 时本地运行：`pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000`

## RFC 1982 判定规则

序列号空间为模 2³² 的环。定义前向模距离：

```
distance = (proposed - current) mod 2^32
```

| 距离 | 关系 `relation` | 含义 |
|---|---|---|
| `0` | `equal` | 相等 |
| `1 ≤ distance < 2^31` | `newer` | 严格更新、可比较 |
| `distance = 2^31` | **`ambiguous`** | 正对点，RFC 1982 明确规定**不可比较** |
| `2^31 < distance < 2^32` | `older` | 回退（等价于 proposed 落后 current） |

`relation` 不是 `newer` 的副本都会列入 `blocking_replicas`（阻塞副本）。

## API

### `POST /audit` — 审计拟发布值

请求：

```json
{
  "proposed": 2024092101,
  "replicas": [
    {"name": "ns-a", "current": 2024092100},
    {"name": "ns-b", "current": 2024092050},
    {"name": "ns-c", "current": 2024092102}
  ]
}
```

响应（每个副本逐一给出关系、前向模距离、是否阻塞；并汇总阻塞副本名）：

```json
{
  "proposed": 2024092101,
  "results": [
    {"name": "ns-a", "current": 2024092100, "proposed": 2024092101,
     "relation": "newer", "forward_distance": 1, "blocking": false},
    {"name": "ns-b", "current": 2024092050, "proposed": 2024092101,
     "relation": "newer", "forward_distance": 51, "blocking": false},
    {"name": "ns-c", "current": 2024092102, "proposed": 2024092101,
     "relation": "older", "forward_distance": 4294967295, "blocking": true}
  ],
  "blocked": true,
  "blocking_replicas": ["ns-c"]
}
```

正对点示例（差值正好 2³¹，必为 `ambiguous`）：

```bash
curl -s http://localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "proposed": 2147483648,
  "replicas": [{"name": "ns-a", "current": 0}]
}'
# relation: "ambiguous", forward_distance: 2147483648, blocking: true
```

回绕示例（`4294967295 -> 0` 是合法的严格推进）：

```bash
curl -s http://localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "proposed": 0,
  "replicas": [{"name": "ns-a", "current": 4294967295}]
}'
# relation: "newer", forward_distance: 1
```

### `POST /propose` — 推导最优候选

在环形空间中寻找一个对**所有副本**都严格更新且可比较（对每个副本均为 `newer`）的候选值，
最小化 **最大前向模距离** `max_i (proposed - current_i) mod 2^32`；
若多个候选同分，取**数值最小**者。

```bash
curl -s http://localhost:8000/propose -H 'Content-Type: application/json' -d '{
  "replicas": [
    {"name": "ns-a", "current": 100},
    {"name": "ns-b", "current": 150}
  ]
}'
```

```json
{
  "proposed": 151,
  "max_forward_distance": 51,
  "results": [
    {"name": "ns-a", "current": 100, "relation": "newer", "forward_distance": 51},
    {"name": "ns-b", "current": 150, "relation": "newer", "forward_distance": 1}
  ]
}
```

当各副本当前序列号跨越半环、不存在任何单一值能同时严格推进时（例如存在相差正好 2³¹ 的正对点，
或最宽跨度达到 2³¹），返回 **409 Conflict**：

```json
{
  "detail": {
    "error": "no_valid_proposal",
    "message": "不存在能对所有副本都严格更新且可比较的候选值：副本当前序列号跨越半环（含相差 2^31 的正对点）"
  }
}
```

### `POST /transfer-plan` — 规划 IXFR / AXFR 传输

给定目标 SOA 序列号、各副本当前值与一组**无序** IXFR 增量（`from_serial`、`to_serial`），
先对增量集合做整体校验，再为每个副本沿增量链逐跳追踪，规划增量传输（IXFR）或全量传输（AXFR）。

**链路匹配遵循 RFC 1982 环形顺序**：只沿增量自身的 `from_serial -> to_serial` 边前进，
边的合法性以环形先后关系判定（`4294967295 -> 0` 是合法推进），**不按整数大小排序**。

- 完全相同的重复增量自动去重；
- 任一增量的 `to_serial` 不是 `from_serial` 的严格更新（`equal` / `older` / 正对点 `ambiguous`），
  返回 **422**（`error=delta_not_strictly_newer`）；
- 同一 `from_serial` 指向不同 `to_serial` 的**分叉**，返回 **422**（`error=delta_fork`）。

每个副本的结果 `mode`：

| `mode` | 条件 | `reason` |
|---|---|---|
| `up_to_date` | 当前值已等于目标 | `null` |
| `ixfr` | 存在从当前值到目标的唯一完整增量链（无序输入会被排序到 `ordered_deltas`） | `null` |
| `axfr` | 链路存在**缺口** `gap`（某序列号没有对应增量） | `"gap"` |
| `axfr` | 沿链行进重复经过某序列号，形成**环路** `loop` | `"loop"` |
| `axfr` | 当前值与目标在 RFC 1982 下**不可比较**（前向距离恰为 2³¹） | `"target_incomparable"` |

> 校验后每个 `from_serial` 至多有一个后继，因此任何完整链都是唯一的。
> 只要存在从副本出发能实际连通目标的完整链（即便链跨过 2³² 回绕点、目标在环上更旧），
> 即为 `ixfr`；连不通才按缺口 / 环路 / 不可比较降级为 `axfr`。

请求（注意增量是乱序的，且含一条重复）：

```bash
curl -s http://localhost:8000/transfer-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 5,
    "replicas": [
      {"name": "ns-a", "current": 5},
      {"name": "ns-b", "current": 1},
      {"name": "ns-c", "current": 7}
    ],
    "deltas": [
      {"from_serial": 4, "to_serial": 5},
      {"from_serial": 1, "to_serial": 2},
      {"from_serial": 3, "to_serial": 4},
      {"from_serial": 2, "to_serial": 3},
      {"from_serial": 2, "to_serial": 3}
    ]
  }'
```

响应（`results` 保持副本的输入顺序）：

```json
{
  "target": 5,
  "results": [
    {"name": "ns-a", "current": 5, "target": 5,
     "mode": "up_to_date", "reason": null, "ordered_deltas": []},
    {"name": "ns-b", "current": 1, "target": 5,
     "mode": "ixfr", "reason": null,
     "ordered_deltas": [
       {"from_serial": 1, "to_serial": 2},
       {"from_serial": 2, "to_serial": 3},
       {"from_serial": 3, "to_serial": 4},
       {"from_serial": 4, "to_serial": 5}
     ]},
    {"name": "ns-c", "current": 7, "target": 5,
     "mode": "axfr", "reason": "gap", "ordered_deltas": []}
  ]
}
```

跨 2³² 回绕的增量链同样按环形顺序排序（`4294967294 -> 4294967295 -> 0 -> 1` 是合法 IXFR 链）；
副本当前值与目标相差正好 2³¹ 时即使有可连通的边也返回 `axfr / target_incomparable`：

```bash
curl -s http://localhost:8000/transfer-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 2147483648,
    "replicas": [{"name": "ns-a", "current": 0}],
    "deltas": [
      {"from_serial": 0, "to_serial": 1},
      {"from_serial": 1, "to_serial": 2147483648}
    ]
  }'
# {"...","mode":"axfr","reason":"target_incomparable","ordered_deltas":[]}
```

### `POST /retention-plan` — 字节预算内的增量保留规划

给定目标 SOA 序列号、一组**带字节数**的无序 IXFR 增量（`from_serial`、`to_serial`、`bytes`）、
各副本当前值与非负 `priority`（**至多 12 个副本**），以及总字节预算 `budget`：
在 `used_bytes <= budget` 的约束下选择要**保留**的增量子集，使
**拥有到目标完整链的副本 priority 总和最大**，并依次按以下规则确定唯一结果：

1. 被覆盖副本（`ixfr` + `up_to_date`）的 **priority 总和最大**；
2. 被覆盖**副本数最多**；
3. **占用字节最少**；
4. 按 `(from_serial, to_serial)` 升序排列的**增量序列字典序最小**。

增量校验复用 `/transfer-plan` 的环形链路规则（非严格 newer、分叉均为 422），并新增：
同一 `(from_serial, to_serial)` 携带**不同 `bytes`** 时返回 **422**（`error=delta_bytes_conflict`）；
完全相同的重复增量去重。副本数超过 12 返回 **422**（`error=too_many_replicas`）。

每个副本的结果 `mode`（复用 `plan_transfer` 在**完整**增量集合上的判定）：

| `mode` | 条件 | `reason` |
|---|---|---|
| `up_to_date` | 当前值已等于目标（不占用预算，恒视为已覆盖） | `null` |
| `ixfr` | 到目标的完整链被保留集覆盖 | `null` |
| `axfr` | 完整集合下即存在**缺口**（保留任何子集都无法连通） | `"gap"` |
| `axfr` | 完整集合下即形成**环路** | `"loop"` |
| `axfr` | 当前值与目标在 RFC 1982 下**不可比较**（前向距离恰为 2³¹） | `"target_incomparable"` |
| `axfr` | 完整集合下存在完整链，但因**预算**未被保留 | `"budget"` |

> 保留集恰为被覆盖副本需求集（各自完整链上的增量）之并——共享链的副本只计一次字节，
> 对覆盖无贡献的增量不会被保留。**预算无法保留任何完整链时仍返回 200**
> （`retained_deltas` 为空、`used_bytes` 为 0，相关副本标记 `axfr / budget`）。

请求（预算 12 恰好保住 `ns-a` 的完整链，`ns-b` 因共享链后缀被一并覆盖）：

```bash
curl -s http://localhost:8000/retention-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 4,
    "budget": 12,
    "replicas": [
      {"name": "ns-a", "current": 1, "priority": 10},
      {"name": "ns-b", "current": 3, "priority": 5},
      {"name": "ns-c", "current": 4, "priority": 100},
      {"name": "ns-d", "current": 7, "priority": 1}
    ],
    "deltas": [
      {"from_serial": 3, "to_serial": 4, "bytes": 6},
      {"from_serial": 1, "to_serial": 2, "bytes": 3},
      {"from_serial": 2, "to_serial": 3, "bytes": 3},
      {"from_serial": 1, "to_serial": 2, "bytes": 3}
    ]
  }'
```

响应（`retained_deltas` 按 `(from_serial, to_serial)` 升序，`results` 保持副本输入顺序）：

```json
{
  "target": 4,
  "budget": 12,
  "used_bytes": 12,
  "retained_deltas": [
    {"from_serial": 1, "to_serial": 2, "bytes": 3},
    {"from_serial": 2, "to_serial": 3, "bytes": 3},
    {"from_serial": 3, "to_serial": 4, "bytes": 6}
  ],
  "results": [
    {"name": "ns-a", "current": 1, "target": 4, "mode": "ixfr", "reason": null},
    {"name": "ns-b", "current": 3, "target": 4, "mode": "ixfr", "reason": null},
    {"name": "ns-c", "current": 4, "target": 4, "mode": "up_to_date", "reason": null},
    {"name": "ns-d", "current": 7, "target": 4, "mode": "axfr", "reason": "gap"}
  ]
}
```

预算不足时（例如上例中 `budget` 改为 11）仍返回 200：

```json
{
  "target": 4,
  "budget": 11,
  "used_bytes": 6,
  "retained_deltas": [
    {"from_serial": 3, "to_serial": 4, "bytes": 6}
  ],
  "results": [
    {"name": "ns-a", "current": 1, "target": 4, "mode": "axfr", "reason": "budget"},
    {"name": "ns-b", "current": 3, "target": 4, "mode": "ixfr", "reason": null},
    {"name": "ns-c", "current": 4, "target": 4, "mode": "up_to_date", "reason": null},
    {"name": "ns-d", "current": 7, "target": 4, "mode": "axfr", "reason": "gap"}
  ]
}
```

### `POST /domain-retention-plan` — 按故障域公平的增量保留规划

在 `/retention-plan` 的基础上为每个副本增加**非空** `failure_domain`（故障域，
如机房 / 可用区），复用完全相同的增量校验、链路原因（`gap` / `loop` /
`target_incomparable` / `budget`）与**最多 12 个副本**限制。
优化目标从“全体 priority 总和”改为**逐故障域 maximin 公平**：

对任一保留子集，先汇总每个 `failure_domain` 中已覆盖（`ixfr` 与 `up_to_date`）
副本的 `priority` 总和，再把各域总和按**升序**排列成向量（向量包含请求中出现的
**全部**域，按域名字典序固定分量顺序；该域无已覆盖副本时分量为 0）。
在 `used_bytes <= budget` 的可行子集中**依次**裁决：

1. 各域已覆盖 priority 总和的**升序向量字典序最大**
   （先抬升覆盖最差的域，即逐域 maximin）；
2. 全体已覆盖副本的 **priority 总和最大**；
3. 已覆盖**副本数最多**；
4. **占用字节最少**；
5. 按 `(from_serial, to_serial)` 升序排列的**增量序列字典序最小**。

> 第 1 级可能压倒“全体总和”：例如三域 `C / P / Q`，预算 20 时，
> 覆盖 P(2+2) 与 Q(3) 得向量 `(0,3,4)`（总和 7），而只覆盖 C(6) 得向量
> `(0,0,6)`（总和 6）——尽管 6 的单域值更大，但最小的非零分量 3 > 0，
> 故仍选择均衡方案。

请求（`failure_domain` 必填且非空）：

```bash
curl -s http://localhost:8000/domain-retention-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 4,
    "budget": 12,
    "replicas": [
      {"name": "ns-a", "current": 1, "priority": 10, "failure_domain": "dc-east"},
      {"name": "ns-b", "current": 3, "priority": 5,  "failure_domain": "dc-west"},
      {"name": "ns-c", "current": 4, "priority": 100,"failure_domain": "dc-west"},
      {"name": "ns-d", "current": 7, "priority": 1,  "failure_domain": "dc-east"}
    ],
    "deltas": [
      {"from_serial": 3, "to_serial": 4, "bytes": 6},
      {"from_serial": 1, "to_serial": 2, "bytes": 3},
      {"from_serial": 2, "to_serial": 3, "bytes": 3}
    ]
  }'
```

响应（`retained_deltas` 与逐副本模式同 `/retention-plan`，逐副本结果回显
`failure_domain`；新增按域名升序的 `domain_summaries`）：

```json
{
  "target": 4,
  "budget": 12,
  "used_bytes": 12,
  "retained_deltas": [
    {"from_serial": 1, "to_serial": 2, "bytes": 3},
    {"from_serial": 2, "to_serial": 3, "bytes": 3},
    {"from_serial": 3, "to_serial": 4, "bytes": 6}
  ],
  "results": [
    {"name": "ns-a", "current": 1, "target": 4, "failure_domain": "dc-east",
     "mode": "ixfr", "reason": null},
    {"name": "ns-b", "current": 3, "target": 4, "failure_domain": "dc-west",
     "mode": "ixfr", "reason": null},
    {"name": "ns-c", "current": 4, "target": 4, "failure_domain": "dc-west",
     "mode": "up_to_date", "reason": null},
    {"name": "ns-d", "current": 7, "target": 4, "failure_domain": "dc-east",
     "mode": "axfr", "reason": "gap"}
  ],
  "domain_summaries": [
    {"failure_domain": "dc-east", "covered_priority": 10, "total_priority": 11},
    {"failure_domain": "dc-west", "covered_priority": 105, "total_priority": 105}
  ]
}
```

`domain_summaries` 中：

- `covered_priority` = 该域最终被覆盖（`ixfr` / `up_to_date`）副本的 priority 总和；
- `total_priority` = 该域**全部**副本的 priority 总和（含因 `budget` / `gap` /
  `loop` / `target_incomparable` 未覆盖者），不随预算变化。

与 `/retention-plan` 一样，预算无法保留任何完整链时仍返回 **200**
（`retained_deltas` 为空、`used_bytes` 为 0；`up_to_date` 副本零成本仍计入
`covered_priority`）。全部裁决键与输出排列均确定，**相同输入恒得相同结果**。

### `POST /rollout-gate` — 上线门禁（quorum 时间窗）

给定基线 SOA 序列号 `baseline`、所需 `quorum`（需同时达到基线的副本数下限，
**正整数**），以及每个副本**按时间排序**的观测序列 `observations`
（`timestamp` 可缺失为 `null`，`serial` 必填）：

- 同一副本**存在的时间戳必须严格递增**：重复返回 422
  （`duplicate_observation_timestamp`），倒序返回 422
  （`observation_timestamp_out_of_order`）；缺失（`null`）的时间戳不参与排序校验，
  副本之间时间戳相同不受影响。
- 相邻观测对按 RFC 1982 逐对判定并标记 `relation`：

  | `relation` | 含义 |
  |---|---|
  | `newer` | 严格推进（**允许跨 2³² 回绕**，如 `4294967295 -> 0`） |
  | `equal` | 序列号未变 |
  | `older` | 回退 |
  | `ambiguous` | 正对点（差值恰为 2³¹，不可比较） |
  | `missing_time_order` | 任一时间戳缺失，无法建立时间顺序 |

- 逐副本输出**首次达到基线之后的有效推进区间** `interval`（闭区间，Unix 秒）：
  从首个等于或严格新于基线的观测起，沿 `newer` / `equal` 且推进后仍达到基线的
  相邻对扩展；遇 `older` / `ambiguous` / `missing_time_order` 或推进后跌破基线
  即终止。无区间时逐副本 `reason` 给出原因：`baseline_not_reached`
  （从未达到基线）或 `missing_time_order`（达到了但首次达到的观测无时间戳，
  无法定位区间）。
- 用**事件扫掠**在各区间端点统计同时处于有效推进区间内的副本数（闭区间端点处
  仍计入），输出 quorum 首次满足时间 `first_satisfied_at`、之后再次跌破时间
  `dropped_below_at`（满足后未再跌破为 `null`）与峰值并发 `peak_concurrent`。
- quorum 从未满足时仍返回 **200**，顶层 `reason` 给出明确原因：
  `quorum_exceeds_replicas`（quorum 超过副本总数）或
  `quorum_never_reached`（峰值并发不足）。

请求：

```bash
curl -s http://localhost:8000/rollout-gate \
  -H 'Content-Type: application/json' \
  -d '{
    "baseline": 2024092101,
    "quorum": 2,
    "replicas": [
      {"name": "ns-a", "observations": [
        {"timestamp": 1727000000, "serial": 2024092100},
        {"timestamp": 1727000060, "serial": 2024092101},
        {"timestamp": 1727000120, "serial": 2024092102}
      ]},
      {"name": "ns-b", "observations": [
        {"timestamp": 1727000030, "serial": 2024092101},
        {"timestamp": 1727000090, "serial": 2024092101}
      ]},
      {"name": "ns-c", "observations": [
        {"timestamp": 1727000200, "serial": 2024092101}
      ]}
    ]
  }'
```

响应（`ns-a` 在 `1727000060` 达到基线，与 `ns-b` 的区间重叠至 `1727000120`，
quorum=2 在该窗口内满足；`ns-c` 的点区间来得太晚）：

```json
{
  "baseline": 2024092101,
  "quorum": 2,
  "satisfied": true,
  "first_satisfied_at": 1727000060,
  "dropped_below_at": 1727000120,
  "peak_concurrent": 2,
  "reason": null,
  "replicas": [
    {"name": "ns-a", "reaches_baseline": true, "first_reached_at": 1727000060,
     "interval": {"start": 1727000060, "end": 1727000120}, "reason": null,
     "transitions": [
       {"index": 1, "from_timestamp": 1727000000, "to_timestamp": 1727000060,
        "from_serial": 2024092100, "to_serial": 2024092101, "relation": "newer"},
       {"index": 2, "from_timestamp": 1727000060, "to_timestamp": 1727000120,
        "from_serial": 2024092101, "to_serial": 2024092102, "relation": "newer"}
     ]},
    {"name": "ns-b", "reaches_baseline": true, "first_reached_at": 1727000030,
     "interval": {"start": 1727000030, "end": 1727000090}, "reason": null,
     "transitions": [
       {"index": 1, "from_timestamp": 1727000030, "to_timestamp": 1727000090,
        "from_serial": 2024092101, "to_serial": 2024092101, "relation": "equal"}
     ]},
    {"name": "ns-c", "reaches_baseline": true, "first_reached_at": 1727000200,
     "interval": {"start": 1727000200, "end": 1727000200}, "reason": null,
     "transitions": []}
  ]
}
```

跨 2³² 回绕的推进是合法的（`4294967294 -> 4294967295 -> 0` 连续 newer）；
缺失时间戳的相邻对标记 `missing_time_order` 并终止该副本的有效推进区间：

```bash
curl -s http://localhost:8000/rollout-gate \
  -H 'Content-Type: application/json' \
  -d '{
    "baseline": 4294967294,
    "quorum": 2,
    "replicas": [
      {"name": "ns-a", "observations": [
        {"timestamp": 1000, "serial": 4294967294},
        {"timestamp": 1010, "serial": 4294967295},
        {"timestamp": 1020, "serial": 0}
      ]},
      {"name": "ns-b", "observations": [
        {"timestamp": 1005, "serial": 4294967295},
        {"timestamp": null, "serial": 0}
      ]}
    ]
  }'
# ns-a 区间 [1000, 1020]（回绕推进）；ns-b 区间止于 [1005, 1005]
# satisfied: true, first_satisfied_at: 1005, dropped_below_at: 1020
```

quorum 无法满足时仍返回 200 并给出明确原因：

```bash
curl -s http://localhost:8000/rollout-gate \
  -H 'Content-Type: application/json' \
  -d '{
    "baseline": 100, "quorum": 3,
    "replicas": [
      {"name": "ns-a", "observations": [{"timestamp": 1, "serial": 100}]},
      {"name": "ns-b", "observations": [{"timestamp": 1, "serial": 100}]}
    ]
  }'
# satisfied: false, reason: "quorum_exceeds_replicas", peak_concurrent: 2
```

### `POST /propagation-slo` — 传播 SLO（逐故障域收敛时延）

给定**按时间严格递增**的发布事件序列（`serial` + `released_at`）、各副本
（携带**非空** `failure_domain`）**按时间严格递增**的观测序列
（`timestamp` 必填 + `serial`），以及非负整数 `deadline_seconds`：

- 对第 i 次发布，**只检查**其到下一次发布前的窗口
  `[released_at[i], released_at[i+1])`（左闭右开；最后一次发布的窗口延伸到
  观测结束）。窗口外的观测不参与本次发布的判定，各窗口的到达状态相互独立、
  互不带入。
- 窗口内每条观测按 RFC 1982 相对本次发布版本分类：

  | 观测值相对发布版本 | 事件 | 效果 |
  |---|---|---|
  | `equal` / `newer` | **到达** | 副本进入已到达状态；其故障域若尚未到达，以该观测时间为域的 `reached_at` |
  | `older` / `ambiguous` | **回退** | 若该副本**已到达**，其到达状态失效，且**所在故障域的本窗口到达状态一并失效**，须等待后续重新到达；未到达副本的回退无影响 |

  同一时间戳的事件**先处理回退再处理到达**（回退只使早于该时刻的到达失效，
  同时刻的到达仍然有效）。观测值严格新于发布版本（含跨 2³² 回绕）同样算到达；
  与发布版本相差正好 2³¹（`ambiguous` 正对点）不算到达。
- 每个故障域的 `reached_at` 为该域**首个有效到达时间**（域内任一副本到达即
  域到达；失效后须由新的到达观测重新确定——同域其他副本仍处于已到达状态
  并不能使域自动恢复）。
- 全部故障域都到达的最早时刻即 `converged_at`（等于各域 `reached_at` 的
  最晚值）；**收敛之后的回退不再影响结果**。
- `latency_seconds = converged_at - released_at`；
  `met_deadline = latency_seconds <= deadline_seconds`。
- 窗口结束仍未覆盖全部域时：`converged_at` 与 `latency_seconds` 为 `null`、
  `met_deadline` 为 `false`，并给出按名称升序的 `missing_domains`
  （未到达域的 `reached_at` 为 `null`）。

请求（两次发布、两个故障域）：

```bash
curl -s http://localhost:8000/propagation-slo \
  -H 'Content-Type: application/json' \
  -d '{
    "deadline_seconds": 300,
    "releases": [
      {"serial": 2024092101, "released_at": 1000},
      {"serial": 2024092102, "released_at": 2000}
    ],
    "replicas": [
      {"name": "ns-a", "failure_domain": "dc-east",
       "observations": [
         {"timestamp": 1000, "serial": 2024092100},
         {"timestamp": 1120, "serial": 2024092101},
         {"timestamp": 2050, "serial": 2024092102}
       ]},
      {"name": "ns-b", "failure_domain": "dc-west",
       "observations": [
         {"timestamp": 1180, "serial": 2024092101},
         {"timestamp": 1900, "serial": 2024092101}
       ]},
      {"name": "ns-c", "failure_domain": "dc-west",
       "observations": [
         {"timestamp": 1250, "serial": 2024092101}
       ]}
    ]
  }'
```

响应（第一次发布两域分别在 1120 / 1180 到达，`converged_at=1180`、
`latency_seconds=180` 达标；第二次发布的窗口 `[2000, +∞)` 内 `dc-west`
没有任何观测，未收敛并列入 `missing_domains`）：

```json
{
  "deadline_seconds": 300,
  "releases": [
    {"serial": 2024092101, "released_at": 1000,
     "converged_at": 1180, "latency_seconds": 180, "met_deadline": true,
     "domains": [
       {"failure_domain": "dc-east", "reached_at": 1120},
       {"failure_domain": "dc-west", "reached_at": 1180}
     ],
     "missing_domains": []},
    {"serial": 2024092102, "released_at": 2000,
     "converged_at": null, "latency_seconds": null, "met_deadline": false,
     "domains": [
       {"failure_domain": "dc-east", "reached_at": 2050},
       {"failure_domain": "dc-west", "reached_at": null}
     ],
     "missing_domains": ["dc-west"]}
  ]
}
```

已到达副本在收敛前回退（`older` / `ambiguous`）会使其域的到达状态失效，
须等待后续重新到达：

```bash
curl -s http://localhost:8000/propagation-slo \
  -H 'Content-Type: application/json' \
  -d '{
    "deadline_seconds": 100,
    "releases": [{"serial": 10, "released_at": 0}],
    "replicas": [
      {"name": "ns-a", "failure_domain": "dc-east", "observations": [
        {"timestamp": 10, "serial": 10},
        {"timestamp": 20, "serial": 9},
        {"timestamp": 50, "serial": 10}
      ]},
      {"name": "ns-b", "failure_domain": "dc-west", "observations": [
        {"timestamp": 30, "serial": 10}
      ]}
    ]
  }'
# dc-east：10 到达 -> 20 回退失效 -> 50 重新到达（reached_at=50）
# dc-west：reached_at=30；converged_at=50，latency_seconds=50 <= 100，met_deadline=true
```

非递增的发布或观测时间返回 **422**：发布时间重复
（`duplicate_release_timestamp`）或倒序（`release_timestamp_out_of_order`）、
同一副本观测时间重复（`duplicate_observation_timestamp`）或倒序
（`observation_timestamp_out_of_order`）；副本名重复
（`duplicate_replica_name`）与空 `failure_domain` 同样返回 422。

### 输入校验（422）

以下情况返回 **422 Unprocessable Entity**：

- 序列号越界：必须为整数且在 `[0, 4294967295]`（即 `[0, 2^32-1]`）；
- 副本名重复（`/audit`、`/propose`、`/transfer-plan`、`/retention-plan`、
  `/domain-retention-plan` 均检查）；
- 副本名为空 / `replicas` 为空数组；
- `/transfer-plan` 额外检查：增量非严格 newer（`delta_not_strictly_newer`）、
  同一 `from_serial` 分叉（`delta_fork`）；
- `/retention-plan` 额外检查：副本数超过 12（`too_many_replicas`）、
  相同增量携带不同 `bytes`（`delta_bytes_conflict`）、
  `bytes` / `priority` / `budget` 为负数或非整数；
- `/domain-retention-plan` 额外检查：在 `/retention-plan` 全部校验之外，
  每个副本的 `failure_domain` 必须为非空字符串（空白字符串同样被拒绝）；
- `/rollout-gate` 额外检查：`baseline` 必须是合法序列号；`quorum` 必须为正整数；
  每个副本至少一条观测，观测 `serial` 必须是合法序列号；`timestamp` 若存在必须是
  非负整数，且同一副本内严格递增——重复（`duplicate_observation_timestamp`）
  或倒序（`observation_timestamp_out_of_order`）均返回 422。
- `/propagation-slo` 额外检查：`deadline_seconds` 必须是非负整数；`releases`
  至少一条，`released_at` 必须是非负整数且各发布之间严格递增——重复
  （`duplicate_release_timestamp`）或倒序（`release_timestamp_out_of_order`）
  返回 422；每个副本的 `failure_domain` 必须为非空字符串（空白字符串同样被
  拒绝）；观测 `timestamp` 必填、非负整数，且同一副本内严格递增——重复
  （`duplicate_observation_timestamp`）或倒序
  （`observation_timestamp_out_of_order`）返回 422。

## 算法说明

`/propose` 中，每个当前值 `c` 的合法候选范围是开弧
`{c+1, …, c+2³¹−1}（mod 2³²）`；所有开弧的交集非空时必为一段连续弧，
最优解必位于弧的最小元素，即某个 `(c + 1) mod 2³²`。
因此只需枚举各副本当前值的“紧邻后继”，检查其对所有副本的前向距离均在 `[1, 2³¹−1]`，
再按 `(最大前向距离, 候选数值)` 取最小即可。

`/transfer-plan` 中，增量先经 `build_delta_chain` 校验去重为
`from_serial -> to_serial` 的单后继映射（故完整链唯一，不存在分叉分支选择）；
`plan_transfer` 从副本当前值出发逐跳取后继，**不做整数排序**：
相等即 `up_to_date`，两端前向距离恰为 2³¹ 即 `target_incomparable`；
否则沿边行进直到抵达目标（`ixfr`，记录沿途有序增量）、
找不到后继（`gap`）或重复经过已访问序列号（`loop`）。
注意严格 newer 的边构成的合法环路只能跨 2³² 回绕（至少 3 条边、各边前向距离之和恰为 2³²），
两条边无法在不违反严格 newer 的前提下成环。

`/retention-plan` 中，带字节数的增量先经 `build_sized_delta_chain` 校验去重为
`from_serial -> to_serial` 映射与 `(from_serial, to_serial) -> bytes` 映射；
`plan_retention` 先在**完整**集合上对每个副本跑 `plan_transfer`：
`up_to_date` 恒被覆盖（零成本），`ixfr` 副本的链上增量即其**需求集**，
`gap` / `loop` / `target_incomparable` 在完整集合下即不可达、原因保持。
由于链是单后继映射，副本被子集覆盖当且仅当其需求集被该子集包含，
因此最优保留集必为某组副本需求集之并；副本数不超过 12，
枚举全部副本子集（至多 2¹² = 4096 个，位掩码按位 OR 求并集）即可精确求得
`(priority 总和, 覆盖副本数, -占用字节)` 字典序最大的子集，
再在同分子集中取增量序列（按 `(from_serial, to_serial)` 升序）字典序最小者。
空子集恒可行（`budget >= 0`），故预算再小也能正常返回 200。

`/domain-retention-plan` 复用同一需求集枚举（与 `plan_retention` 共用
`build_sized_delta_chain` 与逐副本 `plan_transfer` 判定），区别仅在裁决键：
先把请求中出现的 failure_domain 按名称升序固定编号，`up_to_date` 副本的
priority 作为常量并入各域基值；对每个可行子集统计各域被覆盖 priority，
将各域总和**升序排列**为元组，按
`(升序域向量, 全体 priority, 覆盖副本数, -占用字节)` 字典序取最大，
最后再以增量序列字典序最小破平手。域向量包含全部域（未覆盖域分量为 0），
故分量排列不随子集变化；连同增量 / 域的固定排序，相同输入的结果完全可复现。

`/rollout-gate` 中，每个副本的观测序列先做时间戳严格递增校验（缺失时间戳跳过），
再逐对按 RFC 1982 标记关系（任一时间戳缺失的相邻对标记 `missing_time_order`，
不判定序列号关系）。首次达到基线（等于或严格更新，即前向距离 ∈ [0, 2³¹)）的
观测即区间起点，之后仅沿 `newer` / `equal` 且仍达到基线的相邻对扩展区间——
注意一次 `newer` 推进可能跨越大半环后重新跌破基线（如基线 10：
`10 -> 10+2³¹−1 -> 8`，第二步距离 2³¹−1 仍是 newer，但 8 相对基线为 older），
因此每一步都重新校验是否达到基线。quorum 判定用**事件扫掠**：每个闭区间
`[start, end]` 产生 `+1` / `−1` 两个事件，同一时间点先计入 `end` 事件再移除
（闭区间端点处仍算在区间内），扫描得到并发数阶梯序列，首个 `>= quorum` 的
时间点即 `first_satisfied_at`，其后首个 `< quorum` 的时间点即
`dropped_below_at`（只报告第一个满足区间）。

`/propagation-slo` 中，发布时间与观测时间先在路由层校验严格递增；对每次
发布取窗口 `[released_at[i], released_at[i+1])`（最后一次发布延伸到观测
结束）内的全部观测，按 RFC 1982 相对本次发布版本分为**到达**
（`equal` / `newer`）与**回退**（`older` / `ambiguous`）两类事件，按
`(时间戳, 回退优先于到达, 副本名)` 排序后扫描：到达事件使副本进入已到达
状态，并在其域尚未到达时记录域的 `reached_at`；**已到达副本**的回退事件
使其所在域的到达状态失效（`reached_at` 清空），须等待后续到达事件重新
到达——同域其他副本仍处于已到达状态并不能使域自动恢复。同一时间戳先
处理回退再处理到达（回退只使早于该时刻的到达失效）。首次全部域均到达的
时间戳即 `converged_at`（此时必等于各域 `reached_at` 的最大值），扫描随即
停止，之后的回退不再影响结果；窗口结束仍有域未到达则 `converged_at=null`
并列出升序的 `missing_domains`。各窗口的到达状态相互独立、互不带入。

## 测试

核心环形算术不依赖 FastAPI，可直接用标准库运行（另含小位宽空间对暴力穷举的对照测试，
`/transfer-plan` 的去重、分叉、缺口、环路、回绕与正对点测试，
`/retention-plan` 的字节数冲突、共享链并集、三级平局裁决、
`budget` 原因与零预算测试，`/domain-retention-plan` 的逐域 maximin
向量优先于全体总和、域向量锚定与排序、覆盖数 / 字节 / 增量序列平局裁决、
covered/total 汇总与稳定性测试，以及 `/rollout-gate` 的时间戳排序校验、
相邻对 RFC 1982 标记、回绕推进、有效推进区间、事件扫掠 quorum 时间窗
与 422 / OpenAPI 示例测试，以及 `/propagation-slo` 的发布 / 观测时间排序
校验、窗口边界（左闭右开）、逐域到达与回退失效、同时刻回退优先、
收敛后回退忽略、缺失域与 deadline 边界测试）：

```bash
python3 -m unittest discover -s tests -v
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由与异常（/audit、/propose、/transfer-plan、
│   │                  #   /retention-plan、/domain-retention-plan、/rollout-gate、
│   │                  #   /propagation-slo）
│   ├── schemas.py     # 请求/响应模型与 422 校验（含 /rollout-gate 与
│   │                  #   /propagation-slo 的 OpenAPI 示例）
│   └── serial.py      # RFC 1982 环形算术、增量链规划、保留规划、上线门禁
│                      #   与传播 SLO 评估（纯函数，零第三方依赖）
├── tests/test_serial.py
├── tests/test_transfer_plan.py
├── tests/test_retention_plan.py
├── tests/test_domain_retention_plan.py
├── tests/test_rollout_gate.py
├── tests/test_propagation_slo.py
├── requirements.txt
├── Dockerfile
├── compose.yaml
└── README.md
```
