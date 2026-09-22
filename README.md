# 冷链温度合规审计 API

基于 **FastAPI + SQLite** 的冷链运输温度合规审计服务。客户端按批次提交带
ISO 8601 时间戳的温度读数与允许区间，服务端：

1. 按时间排序读数；
2. 用**滞回阈值（hysteresis）**识别连续超温事件，避免临界值附近抖动产生碎片；
3. 将超过采样间隔上限的**缺测**切成独立片段；
4. 返回时间轴上每段的起止时间、峰值、缺测标记与是否合规；
5. 可对审计报告按固定字段顺序生成 **canonical JSON** 并计算 **SHA-256**
   摘要进行**封存（seal）**，之后随时**校验（verify）**当前报告是否被篡改；
6. 支持对批次进行**人工复核（review）**：待复核 → 通过/驳回，驳回后可重新
   打开再次复核；每次操作追加留痕，凭 `request_id` 幂等提交，且**不会改动
   审计报告或封存/校验数据**；
7. 支持对按运输顺序排列的多个批次做**运输交接连续性检查（handoffs）**：
   报告相邻批次间的交接间隔、缺口、重叠与两侧末/首段类型，缺口超阈值或
   任一侧相接片段为缺测时标记交接问题；检查**全程只读**。

默认零配置：SQLite 文件库、WAL 模式、启动自动建表，**无需任何第三方账号**。

---

## 目录结构

```
app/
  main.py       # FastAPI 入口、路由、启动初始化（lifespan 建表）
  audit.py      # 审计核心引擎：滞回状态机 + 缺测切片
  handoffs.py   # 运输交接连续性检查（纯逻辑：间隔/缺口/重叠/缺测侧）
  sealing.py    # canonical JSON 构造与 SHA-256 摘要
  schemas.py    # 请求/响应模型与参数校验
  crud.py       # 批次写入、报告查询、封存校验、人工复核与交接检查
  database.py   # sqlite3 连接、建表脚本、WAL/外键
  config.py     # 环境变量配置
tests/          # pytest 引擎与 API 测试
Dockerfile
docker-compose.yml
requirements.txt
```

## 审计规则

### 允许区间与滞回状态机

每批提交 `low_limit`、`high_limit`（°C）与 `hysteresis`（°C，默认 0.5）。
服务端维护三态状态机：`NORMAL` / `HIGH_EXCURSION` / `LOW_EXCURSION`。

| 当前状态 | 读数条件 | 新状态 |
| --- | --- | --- |
| 正常 | `t > high_limit` | 高温越限 |
| 正常 | `t < low_limit` | 低温越限 |
| 正常 | `low_limit ≤ t ≤ high_limit` | 正常（边界值算正常） |
| 高温越限 | `t ≤ high_limit - hysteresis` | 解除，回正常 |
| 低温越限 | `t ≥ low_limit + hysteresis` | 解除，回正常 |

滞回带的意义：温度在允许边界附近来回抖动时（例如上限 8°C，读数在
7.6 ↔ 8.4 之间），只有回落到 **7.5°C** 以下才解除事件，整段抖动仍归并为
**一个**连续超温片段，不会产生大量碎片。解除后同一读数可立即反向进入另一
越限态。

约束：`low_limit < high_limit`，且 `hysteresis < (high_limit - low_limit) / 2`。

### 缺测切片

读数按时间排序后，若相邻读数间隔**严格大于** `max_interval_seconds`
（默认 900 秒 / 15 分钟；等于上限不算缺测），则在两条读数之间切出一个
独立的 `missing` 片段：

- 起止时间为前后两条读数的时间戳；
- `sample_count = 0`，无峰值；
- 缺测两侧的越限事件**互不合并**，状态机在缺测后复位。

### 片段与合规判定

整个批次时间轴被完整切分为相邻片段，类型为：

- `normal` —— 区间内正常观测，`compliant = true`
- `high_excursion` —— 连续高温越限，`compliant = false`
- `low_excursion` —— 连续低温越限，`compliant = false`
- `missing` —— 采样间隔超限的缺测，`missing = true`、`compliant = false`

批次整体 `compliant` 当且仅当**所有片段**合规。

每个片段包含：`start_time` / `end_time` / `duration_seconds`、`sample_count`、
`peak_temperature_c` 与 `peak_at`（高温事件峰值为最高温、低温事件为最低温；
正常/缺测片段为 null）、`max_temperature_c` / `min_temperature_c`、
`missing`、`compliant`。

### 报告封存摘要（canonical JSON + SHA-256）

审计结果一经提交即不可通过 API 修改。为了事后证明报告未被篡改，可以封存报告：

1. 取完整审计报告（即 `GET /batches/{batch_ref}/report` 的字段集合）；
2. 按**固定字段顺序**构造 canonical JSON——顶层字段顺序与
   `id → external_ref → submitted_at → … → segments` 一致，片段按其
   `index` 升序，片段字段也固定顺序；
3. 所有时间戳统一换算为 **UTC 并以 `Z` 结尾**；紧凑序列化（无多余空白、
   UTF-8、非 ASCII 不转义、禁止 NaN/Infinity）；
4. 对序列化后的字节计算 **SHA-256**，得到十六进制小写摘要。

摘要只依赖已持久化的数据，因此同一报告在任意时间、任意进程中重新计算都
得到完全相同的结果。首次 `seal` 保存摘要与 `sealed_at`（UTC），之后重复
调用**幂等返回同一结果**，封存记录不会被覆盖。`verify` 只读取当前数据重新
计算摘要：与封存摘要一致时 `valid=true`；不一致时 `valid=false`，且**不会
修改任何审计数据或封存记录**。

### 人工复核工作流（review）

每个批次都有一个独立于审计结果的复核状态，初始为 `pending`，复核操作向
`review_events` 表**只追加（append-only）**事件，当前状态由最后一条事件
决定。允许的流转只有：

| 当前状态 | decision | 目标状态 |
| --- | --- | --- |
| `pending` | `approve` | `approved` |
| `pending` | `reject` | `rejected` |
| `rejected` | `reopen` | `pending` |

- `approved` 为终态，不可再操作；`rejected` 只能 `reopen`；
  `pending` 下 `reopen` 不合法。非法流转返回 **409**，且不写入事件。
- `reject` **必须填写非空白** `comment`（参数层校验失败返回 422）；
  `approve` / `reopen` 的 `comment` 可选。
- `request_id` 是调用方生成的**幂等键**（批次内唯一）：同一批次用相同
  `request_id` 重复提交且内容（decision/reviewer/comment）一致时，返回
  **首次写入的原事件（HTTP 200）**，不新增事件；内容不一致则返回 **409**。
  不同批次可复用相同的 `request_id`。
- 复核接口只读写 `review_events`：**不改动批次、读数、片段，也不影响
  seal/verify**——封存摘要保持不变、verify 结论不受复核影响。
- 服务端以 `BEGIN IMMEDIATE` 在事务内先取写锁再判定状态，并发提交不会
  产生重复通过/重复事件。

### 运输交接连续性检查（handoffs）

一次冷链运输常由多个批次（不同车辆/冷库/承运段）接力完成。交接检查读取
各批次**已审计时间轴**，对调用方给出的**按运输顺序排列**的相邻批次，逐对比较
“前一批次 `end_time` → 后一批次 `start_time`”：

- `interval_seconds` = 后一批 `start_time` − 前一批 `end_time`（有符号秒数）；
- `interval > 0` 为**缺口**，秒数同时记入 `gap_seconds`；
- `interval < 0` 为两段时间轴**重叠**，秒数记入 `overlap_seconds`；
- `interval = 0` 为首尾相接；输出统一保留三位小数。

每个交接同时报告前一批**末段**与后一批**首段**的 `segment_type` /
`segment_missing`（类型即 `normal` / `high_excursion` / `low_excursion` /
`missing`）。满足以下任一条件即标记 `handoff_issue = true` 并在
`issue_reasons` 给出原因：

| 原因 | 触发条件 |
| --- | --- |
| `gap_exceeds_threshold` | 缺口**严格大于** `allowed_gap_seconds`（等于阈值不算） |
| `missing_boundary_segment` | 前一批末段或后一批首段为 `missing` 片段 |

- **重叠不视为问题**：同一时间窗双方都有观测，只报告重叠秒数；
  批次自身温度是否合规（越限/缺测）也不在交接检查范围内。
- 顶层 `route_compliant` 当且仅当链上**所有交接**均无问题；
  `issue_count` 为存在问题的相邻对数量。
- 该接口**纯只读**：不写任何表，不改变审计报告、seal/verify 与复核数据。
- 比较使用**已持久化**的审计时间轴（批次起止时间与首/末片段均来自库中数据），
  时间戳统一换算为 UTC。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- SQLite 数据持久化在命名卷 `coldchain-data`（容器内 `/data/coldchain.db`）

容器启动时由应用 lifespan 自动完成建库建表，可直接调用接口。

## 本地运行

需要 Python 3.11+。

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库默认 /data/coldchain.db，可用环境变量改到有权限的目录：
COLDCHAIN_DB_PATH=./data/coldchain.db uvicorn app.main:app
```

## 接口

### `POST /batches` —— 提交批次并完成审计

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `external_ref` | string，可选 | 业务批次号，唯一；重复提交返回 `409`。省略则自动生成 UUID |
| `low_limit` / `high_limit` | number | 允许温度区间（°C），边界值视为正常 |
| `hysteresis` | number，可选 | 滞回阈值，默认 `0.5` |
| `max_interval_seconds` | number，可选 | 采样间隔上限，默认 `900`；严格超过切缺测片段 |
| `readings` | 非空数组 | `{ "timestamp": ISO8601, "temperature": °C }`，顺序随意，服务端排序 |

时间戳建议带时区偏移（如 `+08:00`）；不带时区按 UTC 处理。响应统一输出 UTC
（`Z` 后缀）。

```bash
curl -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "external_ref": "TRUCK-A-001",
    "low_limit": 2.0,
    "high_limit": 8.0,
    "hysteresis": 0.5,
    "max_interval_seconds": 900,
    "readings": [
      {"timestamp": "2026-09-22T08:00:00Z", "temperature": 5.0},
      {"timestamp": "2026-09-22T08:10:00Z", "temperature": 8.4},
      {"timestamp": "2026-09-22T08:20:00Z", "temperature": 7.6},
      {"timestamp": "2026-09-22T08:30:00Z", "temperature": 9.1},
      {"timestamp": "2026-09-22T09:30:00Z", "temperature": 5.0},
      {"timestamp": "2026-09-22T09:40:00Z", "temperature": 1.2}
    ]
  }'
```

上例的片段序列（7.6°C 高于解除线 7.5°C，不切断高温事件）：

```
normal → high_excursion(峰值 9.1°C，3 个样本) → missing(3600s) → normal → low_excursion(1.2°C)
```

### `GET /batches/{batch_ref}/report` —— 查询审计报告

`batch_ref` 可以是批次 `id` 或 `external_ref`；不存在返回 `404`。

```bash
curl http://localhost:8000/batches/TRUCK-A-001/report
```

### `POST /batches/{batch_ref}/seal` —— 封存报告摘要

对指定批次按固定字段顺序与 UTC 格式生成 canonical JSON 并计算 SHA-256
摘要。**首次**调用保存摘要与 `sealed_at`；之后重复调用幂等，返回完全相同
的结果（`sealed_at` 也保持首次封存时间）。批次不存在返回 `404`。

```bash
curl -X POST http://localhost:8000/batches/TRUCK-A-001/seal
```

响应：

| 字段 | 说明 |
| --- | --- |
| `id` / `external_ref` | 批次 ID 与业务批次号 |
| `digest_algorithm` | 摘要算法，固定为 `sha256` |
| `digest` | canonical JSON 的 SHA-256（64 位十六进制小写） |
| `sealed_at` | 首次封存时间，UTC（`Z` 后缀） |
| `canonical` | 参与摘要计算的 canonical 报告文档（固定字段顺序、UTC 时间） |

### `GET /batches/{batch_ref}/verify` —— 校验封存摘要（只读）

重新读取当前批次数据计算摘要，并与封存时保存的摘要比对：

| 字段 | 说明 |
| --- | --- |
| `digest` | 由**当前**报告重新计算出的摘要 |
| `sealed_digest` | 封存时保存的摘要；未封存时为 `null` |
| `sealed_at` | 封存时间；未封存时为 `null` |
| `valid` | 当前摘要与封存摘要一致时为 `true` |

- 一致：`valid = true`；
- 报告被改动：`digest != sealed_digest`，`valid = false`；
- 批次从未封存：`sealed_digest` / `sealed_at` 为 `null`，`valid = false`；
- 批次不存在：`404`。

该接口**只读**：无论结果如何都不会修改批次、读数、片段或封存记录。

```bash
curl http://localhost:8000/batches/TRUCK-A-001/verify
# {"...": "...", "digest": "…", "sealed_digest": "…", "sealed_at": "…", "valid": true}
```

### `POST /batches/{batch_ref}/review` —— 提交人工复核结论

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `reviewer` | string，必填 | 复核人标识（1–200 字符，非空白） |
| `decision` | string，必填 | `approve` / `reject` / `reopen` |
| `comment` | string，可选 | 复核意见；`decision=reject` 时**必填非空白** |
| `request_id` | string，必填 | 调用方生成的幂等键；同批次重复提交返回原事件 |

合法流转：`pending → approved/rejected`、`rejected → pending`（reopen，
之后可再次复核）。非法流转返回 `409`；批次不存在返回 `404`；参数校验失败
返回 `422`。新事件返回 `201`，幂等重放返回 `200`。

```bash
curl -X POST http://localhost:8000/batches/TRUCK-A-001/review \
  -H 'Content-Type: application/json' \
  -d '{
    "reviewer": "qa-li",
    "decision": "reject",
    "comment": "存在高温越限片段，需运输方说明",
    "request_id": "7f3c2e9a-01"
  }'
```

响应（单条复核事件）：

| 字段 | 说明 |
| --- | --- |
| `seq` | 批次内事件序号，从 1 自增 |
| `decision` | 本次操作：`approve` / `reject` / `reopen` |
| `from_status` / `to_status` | 操作前后的复核状态 |
| `reviewer` / `comment` | 复核人与意见（无意见为 `null`） |
| `request_id` | 幂等键原样返回 |
| `created_at` | 事件时间，UTC（`Z` 后缀） |

用相同 `request_id` 重发同一请求会原样返回上面的事件（HTTP 200）；若改了
`decision` / `reviewer` / `comment` 再发，则返回 `409`，首次事件不受影响。

### `GET /batches/{batch_ref}/review` —— 查询复核状态与历史（只读）

返回当前 `status`（`pending` / `approved` / `rejected`；从未复核时为
`pending`、`events` 为空）以及 `events` 列表，**按时间（seq）升序**排列，
字段同上。批次不存在返回 `404`。

```bash
curl http://localhost:8000/batches/TRUCK-A-001/review
# {
#   "id": "…", "external_ref": "TRUCK-A-001", "status": "rejected",
#   "events": [ {"seq": 1, "decision": "reject", "from_status": "pending",
#                "to_status": "rejected", …} ]
# }
```

### `POST /handoffs/check` —— 运输交接连续性检查（只读）

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `batch_refs` | string 数组，必填 | **按运输顺序排列**的批次引用（ID 或业务批次号）；至少 2 项、不可重复，最多 500 项 |
| `allowed_gap_seconds` | number，可选 | 允许的最大交接间隔（秒），默认 `900`；缺口**严格大于**该值即标记问题 |

- 任一引用不存在返回 **404**（detail 中给出未找到的引用）；
- 列表不足两项、包含重复引用（含 ID 与 external_ref 指向同一批次）、
  或 `allowed_gap_seconds ≤ 0`，返回 **422**。

```bash
curl -X POST http://localhost:8000/handoffs/check \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_refs": ["TRUCK-A-001", "TRUCK-A-002", "TRUCK-A-003"],
    "allowed_gap_seconds": 900
  }'
```

响应：

| 字段 | 说明 |
| --- | --- |
| `batch_refs` | 回显请求中的批次引用（保持运输顺序） |
| `allowed_gap_seconds` | 本次检查使用的阈值 |
| `handoff_count` | 相邻交接数量（等于批次数 − 1） |
| `issue_count` | 存在交接问题的相邻对数量 |
| `route_compliant` | 所有交接均无问题时为 `true` |
| `handoffs` | 每个相邻对的判定，按运输顺序排列 |

`handoffs[]` 中每项：

| 字段 | 说明 |
| --- | --- |
| `index` | 交接序号，从 0 开始 |
| `from` / `to` | 前一批末段侧 / 后一批首段侧：`batch_id`、`external_ref`、`boundary_time`、`segment_type`、`segment_missing` |
| `from_end_time` / `to_start_time` | 交接两侧边界时间，UTC（`Z` 后缀） |
| `interval_seconds` | 有符号交接间隔：`to.start_time − from.end_time`（秒） |
| `gap_seconds` / `overlap_seconds` | 非负的缺口秒数 / 重叠秒数，两者至多一个非零 |
| `handoff_issue` | 缺口超阈值或任一侧相接片段为 missing 时为 `true` |
| `issue_reasons` | 原因列表：`gap_exceeds_threshold` / `missing_boundary_segment` |

```bash
# 节选：前一批 08:20 结束、后一批 09:35 开始 → 3600s 缺口超限
# {
#   "index": 1,
#   "from": {"external_ref": "TRUCK-A-002", "segment_type": "normal", …},
#   "to":   {"external_ref": "TRUCK-A-003", "segment_type": "normal", …},
#   "interval_seconds": 3600.0, "gap_seconds": 3600.0, "overlap_seconds": 0.0,
#   "handoff_issue": true, "issue_reasons": ["gap_exceeds_threshold"]
# }
```

### `GET /batches` —— 批次列表（摘要）

查询参数：`compliant=true|false`（可选过滤）、`limit`（1–200，默认 50）、
`offset`（默认 0）。

### `GET /health` —— 健康检查

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COLDCHAIN_DB_PATH` | `/data/coldchain.db` | SQLite 文件路径，目录自动创建 |
| `COLDCHAIN_DEFAULT_HYSTERESIS` | `0.5` | 请求未指定时的默认滞回阈值（°C） |
| `COLDCHAIN_DEFAULT_MAX_INTERVAL` | `900` | 请求未指定时的默认采样间隔上限（秒） |
| `COLDCHAIN_DEFAULT_HANDOFF_GAP` | `900` | 交接检查未指定 `allowed_gap_seconds` 时的默认允许交接间隔（秒） |
| `COLDCHAIN_MAX_READINGS` | `100000` | 单批读数上限 |
| `COLDCHAIN_MAX_HANDOFF_BATCHES` | `500` | 单次交接检查的批次数量上限 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖：排序、滞回不分裂抖动事件、边界严格比较、缺测独立切片
（含“等于上限不算缺测”）、高/低温峰值、单读数片段、API 提交/报告/重复 409/
404/422/列表过滤与时区归一化；封存摘要的固定字段顺序、UTC 归一化、重复 seal
幂等、verify 一致/篡改后 `valid=false` 且不改写审计数据、未封存与按 ID 定位；
人工复核的合法/非法状态流转（409）、reject 必填 comment（422）、request_id
幂等重放（200）与内容冲突（409）、历史按时间排序、按 ID 定位，以及复核全程
不改动审计报告与 seal/verify 数据；交接检查的间隔/缺口/重叠/零间隔与毫秒舍入、
末段首段类型报告、缺口等于阈值不算问题、两侧相接 missing 片段标记
`missing_boundary_segment`、原因排序、时区归一化、404/422（不足两项、重复引用、
ID 与 external_ref 同批次）、默认阈值、按 ID 定位，以及检查全程只读不影响
报告/seal/verify/复核。

## 设计说明

- **写时审计**：提交时一次性计算并持久化片段，报告查询为纯读取，结果可追溯。
- **报告封存**：canonical JSON 只依赖持久化数据，字段顺序在 `app/sealing.py`
  中固定，序列化规则确定（紧凑、UTF-8、UTC `Z`），SHA-256 可在任意端独立
  复算；封存在独立的 `batch_seals` 表中，一经写入不可通过接口覆盖，verify
  全程只读。
- **人工复核**：复核状态不存可变字段，而由只追加的 `review_events` 末条事件
  推导，历史天然完整可审计；状态机在 `app/crud.py` 中集中定义，非法流转与
  幂等键内容冲突一律 409；事务以 `BEGIN IMMEDIATE` 串行化并发判定。复核数据
  与审计/封存数据物理分表，复核路径不写 `batches` / `readings` / `segments`
  / `batch_seals`，封存摘要与 verify 结论不受影响。
- **交接连续性检查**：判定逻辑为 `app/handoffs.py` 中的纯函数（输入各批次
  时间轴边界与首/末片段，输出每个相邻对的间隔、缺口、重叠与问题原因），
  `app/crud.py` 只负责只读装载；**不新增数据表、不持久化任何结果**，对审计
  报告、封存与复核数据零影响。缺口使用未舍入秒差做严格大于比较，重叠只报告
  不判问题；`route_compliant` 仅刻画“交接连续性”，与批次自身温度合规相互独立。
- **SQLite**：标准库 `sqlite3`，开启 WAL（并发读不阻塞写）与外键级联删除；
  批次、读数、片段、封存、复核事件五张表并建立索引；新表以幂等 DDL 在启动时
  创建，旧版本数据库升级无需手工迁移。
- **纯函数引擎**：`app/audit.py` 不依赖 Web/DB 框架，输入排序后的读数，
  输出片段列表，便于独立测试与复用。
- **不做读数去重**：相同时间戳按提交顺序稳定排列、全部保留。
