# 巡检趋势（trend）能力

`linkpatrol trend` 是一个**离线、只读**的多 run 分析能力：它读取同一个 seed 下
的若干历史 run，把每个规范化 URL（以及页面锚点）在时间轴上对齐，输出状态码、
重定向链长度、锚点结论、外部链接失败数等指标的走势，并判定两类异常——
**持续恶化（deteriorating）** 与 **波动性抖动（flapping）**。

本能力不修改任何 scan 写入路径：scan 数据库始终以 SQLite `mode=ro` 只读 URI
打开，趋势产物写入独立的趋势数据库（默认 `out/trends.db`）。

## 命令

```bash
python -m linkpatrol trend generate \
  --db out/linkpatrol.db --trend-db out/trends.db \
  --seed http://127.0.0.1:5001/ \
  --since 2026-01-01T00:00:00+00:00 --until 2026-12-31T23:59:59+00:00 \
  --min-occurrences 2 [--include-incomplete] [--runs 1,2,3] [--json-out out/trend.json]
python -m linkpatrol trend list [--trend-db out/trends.db] [--seed ...]
python -m linkpatrol trend show --trend-db out/trends.db --report 1
```

* `--seed`：必填。只纳入该 seed 的 run。
* 时间窗口：`--since` / `--until` 对 `runs.started_at` 做包含式过滤（ISO-8601
  字符串比较，因此建议统一带时区的完整时间戳）。
* `--runs 1,2,3`：显式指定 run id，与时间窗口互斥；未知 id 会报错而非静默忽略。
* `--min-occurrences`：最小出现次数阈值（默认 2）。观测 run 数低于该值的实体
  仍写入明细序列，但 `eligible=false`、`classification=below_threshold`，
  不参与恶化/抖动判定。
* `--include-incomplete`：纳入 `status != 'completed'` 的中断 run（见下）。

## 跨 run 的实体身份模型

URL 在不同 run 里会被当时的 `normalize.drop_params` 规则改写，因此单一字符串
身份不可靠。本能力使用两级身份键：

| 键 | 构成 | 用途 |
| --- | --- | --- |
| 精确键 `exact` | `scheme://host[:port]/path?query`（去 fragment） | 最强身份，即 run 自身规范化后的 URL |
| 结构键 `structural` | `scheme://host[:port]/path`（去 query 与 fragment） | 跨 normalize 规则稳定 |

对齐规则（对所有选中 run 一次性、全局地执行，保证结果与 run 到达顺序无关）：

1. 一个结构键在全部 run 中只对应**一个**精确 URL：合并为一个实体，
   `alignment` 为 `exact`（无 query）或 `structural`（带过 query），
   所有历史精确 URL 记录在 `aliases` 里。这解决了 `drop_params` 变更后
   同一页面以新身份出现的问题——例如 run 1 的 `/landing?campaign=home` 与
   run 4 的 `/landing` 会对齐到同一实体。
2. 一个结构键在**同一个 run 内**对应多个共存的精确 URL（说明该站点确实存在
   仅靠 query 区分的不同页面）：判定为**歧义**，`alignment=ambiguous`，
   拒绝合并、每个精确 URL 各自独立成实体。**对齐失败时降级，绝不猜测身份。**
3. 不同 run 中各出现一个 query 变体、但从不在同一 run 共存的，按规则 1 合并。

锚点实体的身份是 `(页面结构键, fragment)`，只挂在已成功对齐的页面实体上；
若页面本身无法对齐，锚点不会被重新安置到别的页面，而是标记 `unaligned:`
前缀单独保留。

### 序列里的覆盖语义（避免把「没扫到」误判成「消失」）

每个实体在每个 run 上都有一个点，未实际观测的点用 `coverage` 区分：

| coverage | 含义 |
| --- | --- |
| `observed` | 该 run 的 pages 或 checks 中存在此 URL |
| `missing` | run 完整结束，且确实没有这个 URL——真实的缺失 |
| `unscanned` | run 被 `max_pages` 截断（`stats.crawl.skipped_by_limits > 0`）或中断，且该结构路径不在本 run 已观测集合内——无法断言存在与否 |

截断判定是**按结构路径精确化**的：即使一个 run 被截断，只要它已经抓到过该
结构键下的某个页面，那么同结构键下另一个 query 变体的缺失仍然是真实 `missing`，
而不是 `unscanned`。`unscanned` 的缺口永远不会产生 `disappeared` 事件。

### 中断 run 的处理

* 默认只纳入 `status='completed'` 的 run。
* `--include-incomplete` 时中断 run 也进入时间轴，`runs[].status` 原样标注、
  汇总里计入 `runs_incomplete`，其所有未观测缺口一律按 `unscanned` 处理，
  不计入恶化/消失。
* 匹配 run 少于 2 个时直接报错（`TrendError`），不产出无意义的单点报告。

### run 内 checks 与 pages 对不上怎么办

重试与重定向会让两张表的记录不完全一致。合并观测值时：

* URL 出现在任一表即视为 `observed`；带 fragment 的 check 折叠到其基础 URL
  的页面观测上（锚点结论单独建锚点实体）。
* 两表都有时，**check 行**对 `verdict/status_code/redirect_chain` 优先（它是
  验证结论），**page 行**提供 `content_hash` 并补齐 check 缺失字段。
* 外部链接失败数由 `links JOIN checks`（`scope='external' AND ok=0`）按来源
  页面聚合 `COUNT(DISTINCT target_url)`。

## 指标定义（每个实体、每个 run 一个点）

| 字段 | 定义 |
| --- | --- |
| `status_code` | check 行优先、否则 page 行的最终 HTTP 状态码；网络错误时为 `null` |
| `status_class` | `2xx` / `3xx` / `4xx` / `5xx` / `network_error`（有 error 无状态码）/ `unknown` / `other` |
| `verdict` | `ok` / `broken`，取自 check 结论（无 check 时由状态码与 error 推导） |
| `severity` | 严重度 0–3，见下表 |
| `redirect_chain_length` | 重定向链跳数（`redirect_chain` 数组长度），无重定向为 0 |
| `anchor_verdict` | 页面实体：`not_applicable` / `fail`；锚点实体：`ok` / `fail` / `missing` / `unscanned` / `unevaluated` |
| `external_failures` | 该页面作为来源、指向的失败外部链接去重目标数 |
| `content_hash` | page 行的响应体 SHA-256；实体级 `content_changed=true` 表示不同 run 哈希不一致 |

严重度 `severity`：

| 级别 | 触发条件 |
| --- | --- |
| 0 `SEV_OK` | 2xx/3xx 且无锚点失败 |
| 1 `SEV_MINOR` | 存在失败的外部链接（自身仍可访问） |
| 2 `SEV_MAJOR` | 4xx，或锚点缺失（`anchor_missing`） |
| 3 `SEV_CRITICAL` | 5xx 或网络错误（含重定向循环/超限） |

锚点的特殊取值：页面抓取本身失败（4xx/5xx/网络错误）时锚点**无法评估**，
记为 `unevaluated`，既不算 `ok` 也不算 `fail`；页面 2xx 但 fragment 目标
不存在才记 `fail`。

## 异常分类

只对 `eligible=true`（观测 run 数 ≥ `min_occurrences`）的实体，按**观测点之间**
的严重度转移分类；`missing`/`unscanned` 点不参与严重度比较。

| classification | 判定 |
| --- | --- |
| `deteriorating` 持续恶化 | 至少一次严重度上升、末态严重度高于首态且 ≥1，并且**从未恢复到健康（0）**。在不健康区间内的变化（如 5xx→4xx）不抵消恶化 |
| `flapping` 波动性抖动 | 曾从不健康恢复到健康（严重度回到 0），随后又再次恶化 |
| `stable_unhealthy` | 末态 ≥1 但不满足上面两条（一直不健康、无净恶化） |
| `improving` | 只有严重度下降、无上升，且末态健康 |
| `stable_healthy` | 观测期内严重度无变化且健康 |
| `below_threshold` | 观测 run 数低于阈值，只入明细 |

实体级还有两个布尔辅助标记：`redirect_chain_grew`（末跳数 > 首跳数）与
`content_changed`（内容哈希发生过变化）。锚点实体使用同一套分类标签，但只
基于 `ok/fail` 转移（`unevaluated` 不参与）。

## 事件

* `appeared`：相对更早的观测空档新出现（首个选中 run 即存在的实体算基线，
  不发 appeared）。
* `returned`：经历过真实 `missing` 后再次被观测到。
* `disappeared`：在完整 run 中由 `observed` 变为 `missing`；`unscanned` 缺口
  不会触发。
* 锚点事件：`anchor_broke` / `anchor_fixed` / `anchor_gone`（页面仍在但该
  fragment 的链接整段消失）。

`events.appeared` 与 `events.disappeared` 在报告顶层单独成列，方便直接回答
「哪些 URL 消失了 / 哪些是新出现的」。

## 持久化与可重复性

趋势库 `out/trends.db` 含四张表：`trend_reports`、`trend_entities`、
`trend_series`、`trend_events`。

* 报告身份由除 `generated_at`/`report_id` 外的全部内容做规范化 JSON
  （键排序、无空白）SHA-256 得到 `content_hash`，并对其建唯一约束。
* 对同一批 run、同一组参数重复 `generate` 会命中同一哈希，只刷新
  `generated_at`，不新增行、不改变任何实体结论——**可重复生成且结果一致**。
* 追加新的历史 run 后再次运行是一次新的分析（run 集合变了，哈希也变），
  旧报告原样保留；指标定义随 `schema_version` 演进，本文档是其唯一权威定义，
  修改定义必须提升版本号，避免破坏既有结论的可解释性。

## 内置演示

`scripts/demo_trends.py` 在本地启动内置 demo 站并串行执行 4 次 scan：
健康基线 → `/docs` 500（抖动来源）→ `/about` 持续 404（恶化来源）→
改变 `drop_params` 且 `max_pages=6` 截断（身份对齐与 unscanned 演示），
最后生成趋势报告。故障通过 `LINKPATROL_DEMO_FAULTS` 环境变量注入一个
`{路径: 状态码}` JSON，缺省时 demo 站行为与原来完全一致。

```bash
.venv/bin/python scripts/demo_trends.py --keep-db
```
