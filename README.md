# 巡链台（linkpatrol）

`linkpatrol` 是一个完全本地、可离线运行的网站链接健康巡检与站点地图工具。它从
seed 出发做**有界**爬取，构建链接图，检查链接、资源、重定向与锚点，把结果写入
SQLite，生成 JSON/HTML 报告与 sitemap.xml，并支持把“新增断链”作为 CI 回归信号。

所有演示与测试目标都由仓库内置的 Flask 站点提供，默认地址仅为
`http://127.0.0.1:5001/`。运行和测试不需要访问外网。

## 这不是什么

- 不是通用数据记录采集管道：不抽取业务字段，不产出结构化业务数据集。
- 不是页面内容变更监控：不保存/对比页面快照，不做内容 diff 告警。
- 不是表单填报或 RPA：不登录、不提交表单、不执行写操作。
- 不是 SEO 评分/排名、安全扫描、性能压测或 BI 看板工具。

## 合规声明

- 只扫描你拥有或已获得书面授权的站点。对目标站点只读，绝不写入。
- 默认遵守 robots.txt，默认串行并支持 `delay_ms` 限速。
- 禁止用本项目对第三方服务进行高并发冲击。请自行承担使用责任。
- 示例配置、域名和地址全部为 `127.0.0.1`，不包含真实密钥或公司信息。

## 架构

```text
 scan.toml
    |
    v
 config.py ---> crawler.py (BFS, scope/limits/robots, urlnorm, fetcher)
                     |                        |
                     |                        +--> extractor.py (links/anchors)
                     v
               checker.py (reuse crawl, HEAD->GET, redirects, anchors)
                     |
                     v
                  store.py (SQLite, WAL, unique constraints)
                    / \
                   v   v
       report.py        baseline.py
    JSON + HTML reports  diff / ignore / exit code 2
       |       |
       v       v
 sitemap.py  server.py (read-only Flask UI)
```

设计取舍：抓取、HTML 解析、配置、存储与 CLI **刻意只使用标准库**；Flask 仅用于
报告浏览服务和内置演示站点（Jinja2 随 Flask 提供）。这样安装面最小、行为确定、
离线可复现，也避免 requests/bs4/浏览器引擎等重依赖。

## 数据模型

SQLite 开启外键与 WAL。写入按行使用 savepoint；单条失败回滚该条并记录错误。

| 表 | 字段 | 唯一约束 |
| --- | --- | --- |
| `runs` | `id, seed, config_json, started_at, finished_at, status, stats_json` | 主键 `id` |
| `pages` | `id, run_id, url, depth, status_code, content_hash, duration_ms, error` | `(run_id, url)` |
| `links` | `id, run_id, source_url, target_url, raw_href, kind, scope, line_no, anchor` | `(run_id, source_url, target_url, kind)` |
| `checks` | `id, run_id, url, final_url, status_code, redirect_chain_json, anchor_ok, ok, error, checked_at` | `(run_id, url)` |
| `attempt_details` | `id, run_id, target_url, phase, attempts, retry_count, first_failure_code, final_failure_code, budget_skipped, error` | `(run_id, target_url, phase)` |
| `baselines` | `name, run_id, created_at` | 主键 `name` |

`pages` 记录规范化 URL、深度、状态、响应体 SHA-256、耗时与错误；`links` 保留下
载目标、原文、类型 (`page/asset/form`)、范围 (`internal/external/anchor/mailto/tel/other`)
和行号；`checks` 保存最终 URL、重定向链、锚点结果和结论。每次 `scan` 创建新
run，不覆盖历史。

`attempt_details` 是 run 级重试预算与失败归因明细：只有发生过重试、失败或被预算
截断的 target 才会落行，记录所属阶段 (`crawl`/`check`)、实际尝试次数、重试次数、
首次/最终失败码（网络错误为 `NULL`）以及 `budget_skipped` 标记。旧版本数据库没有
该表，打开时会自动补建；旧 run 没有明细时报告的失败分布降级为空结构，不会报错。

## 配置

TOML 配置在默认值基础上合并，未知键、类型错误或非法正则都会导致退出码 3。

| 分组 | 键 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 顶层 | `seed` | 必填 | 起始 http(s) URL |
| `scope` | `allow_subdomains` | `false` | 是否允许 seed 的子域 |
| `scope` | `include` / `exclude` | `[]` | 作用于 path 的正则白/黑名单 |
| `limits` | `max_depth` | `3` | seed 深度为 0 |
| `limits` | `max_pages` | `100` | 页面数上限；截断计入 skipped |
| `limits` | `timeout_s` | `5.0` | 单请求超时秒数 |
| `limits` | `retries` | `2` | 网络错误/5xx 指数退避重试次数（4xx 不重试）|
| `limits` | `delay_ms` | `0` | 请求间隔毫秒 |
| `limits` | `concurrency` | `1` | 1–8；默认串行保证确定性 |
| `limits` | `max_redirects` | `5` | 最大重定向跳数 |
| `limits` | `max_attempts` | `null`（自动推导）| run 级总请求尝试上限；缺省为 `max_pages × 2 × (retries+1) + 10`，显式整数可覆写 |
| `robots` | `respect` | `true` | 是否遵守 robots.txt |
| `robots` | `user_agent` | `linkpatrol/0.1` | 请求 UA 与 robots 组匹配 |
| `external` | `check` | `true` | 是否检查站外链接 |
| `normalize` | `drop_params` | `[]` | 通配符参数列表，如 `utm_*` |
| `output` | `dir` | `reports` | JSON/HTML 报告目录 |
| `output` | `database` | `out/linkpatrol.db` | SQLite 路径 |
| `output` | `sitemap` | `out/sitemap.xml` | 生成的 sitemap 路径 |

完整示例见 `examples/scan.demo.toml`：seed 为 `http://127.0.0.1:5001/`，
`normalize.drop_params = ["utm_*", "fbclid"]`，`exclude = ["^/search"]`。
忽略规则见 `examples/ignores.example.toml`，URL 清单见 `examples/urls.example.txt`。

```toml
seed = "http://127.0.0.1:5001/"

[scope]
allow_subdomains = false
include = []
exclude = ["^/search"]

[limits]
max_depth = 3
max_pages = 60
timeout_s = 3.0
retries = 2
delay_ms = 20
concurrency = 1
max_redirects = 5

[robots]
respect = true
user_agent = "linkpatrol-demo/0.1"

[external]
check = true

[normalize]
drop_params = ["utm_*", "fbclid"]

[output]
dir = "reports"
database = "out/linkpatrol.db"
sitemap = "out/sitemap.xml"
```

## 命令行

```bash
python -m linkpatrol demo --port 5001
python -m linkpatrol scan --config examples/scan.demo.toml
python -m linkpatrol report --db out/linkpatrol.db --run 1 --out reports
python -m linkpatrol serve --db out/linkpatrol.db --port 5002
python -m linkpatrol sitemap --db out/linkpatrol.db --run 1 --out out/sitemap.xml
python -m linkpatrol sitemap --parse-url http://127.0.0.1:5001/sitemap.xml
python -m linkpatrol sitemap --parse-file out/sitemap.xml
python -m linkpatrol baseline set --name prod --run 1 --db out/linkpatrol.db
python -m linkpatrol baseline list --db out/linkpatrol.db
python -m linkpatrol diff --baseline prod --run 2 --db out/linkpatrol.db \
  --ignore examples/ignores.example.toml --fail-on new-broken
```

`scan` 还接受 `--urls-file` 或 `--sitemap-url` 指定孤儿页对比清单；未指定时会尝试
seed 源站的 `/sitemap.xml`。`serve` 提供 `/`、`/runs/<id>`（按状态/类型过滤）和
`/pages/<id>`，全部服务端渲染，CSS 内联，不加载任何外部 JS/CSS/字体。

退出码：

| 码 | 含义 |
| --- | --- |
| `0` | 成功；diff 未发现新增断链 |
| `1` | 运行期错误（文件、数据库、抓取流程等） |
| `2` | `--fail-on new-broken` 且存在新增断链 |
| `3` | 配置错误 |

`diff` 的断链身份是 `(source_url, target_url)`，输出 `new_broken/fixed/persistent`
三类 JSON，并把最近一次摘要写入当前 run 的 `stats_json.last_diff`。忽略规则只在
判定 `new_broken` 时生效。

## Run 级重试预算与失败归因

`retries` 只约束**单次请求**的指数退避次数；一个 run 里多个不稳定 target 叠加时
总尝试次数仍可能失控。`max_attempts` 在其之上再加一层 run 级总预算，由 crawler
与 checker 共享同一个线程安全计数器（并发下原子预留，绝不超支）：

- 预算耗尽后，剩余 URL 不再发起请求，直接标记为 `skipped_budget`
  （check 记录的 `error` 为 `budget_exhausted`，计入 broken），run 走干净的部分
  完成路径并在可预期时间内结束。
- `runs.status` 三态区分：`completed`（无预算截断且无抓取级错误）、
  `partial`（完整跑完但存在抓取错误，如持续 5xx）、`budget_exhausted`
  （预算截断）；异常中断仍为 `failed`。状态变化不改变 CLI 退出码语义，截断的
  scan 依然返回 `0`。
- `stats_json.budget` 记录 `limit/used/denied/remaining/exhausted`，
  `stats_json.skipped_budget` 记录跳过数。
- JSON 报告的 `failure_distribution` 输出按失败码（`404`/`500`/`network_error`/
  `budget_exhausted`）和按 target 聚合的失败分布，HTML 报告在有明细时渲染
  “Retry budget and failure distribution”区块；无明细的旧 run 降级为空。

## 快速上手

```bash
# 1. 安装（离线环境请使用本机 wheel 缓存）
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt

# 2. 启动演示站点
python -m linkpatrol demo --port 5001

# 3. 另开终端执行扫描（生成 out/ 与 reports/）
python -m linkpatrol scan --config examples/scan.demo.toml

# 4. 浏览报告并运行测试
python -m linkpatrol serve --db out/linkpatrol.db
python -m pytest
```

演示站点包含多级正常页、404/500、三级重定向、重定向循环、慢响应、前两次 500 后
恢复、存在/缺失锚点、robots Disallow、含孤儿页的 sitemap、无出链死胡同、图片/
脚本/样式资源与缺失资源。第二个外站 mock 站点覆盖 HEAD 405 → GET 回退。

## 检查结论与限制

| 结论 | 判定条件 |
| --- | --- |
| `ok` | 最终响应 2xx；若链接带 fragment，目标页锚点集合中存在该锚点 |
| `broken` | 最终 4xx/5xx、网络错误、重定向循环、超过最大跳数、锚点缺失 |
| `warning` | 最终 2xx，但记录了 ≥3 跳重定向链 |

站内 2xx HTML 页面链接直接复用爬取结果，不重复请求；站内资源与站外链接先 HEAD，
返回 405/501 时回退 GET。目标为已抓取 2xx HTML 页时才校验 fragment。报告仅统计，
不做 AI 原因推断或自动修复建议。

## 测试

测试使用 pytest。`tests/conftest.py` 的 session 级 socket 守卫拒绝任何非
`127.0.0.1` 的连接和 DNS 查询；mock 站点用随机端口启动，测试结束回收。覆盖
URL 规范化、五类标签提取、robots 优先级、重试/超时/重定向、深度与页数上限、
robots 跳过、HEAD 回退、锚点、唯一约束与回滚、sitemap 递归、三类孤儿发现、基线
分类、报告必填键、Flask 路由和完整 CLI 回归流程（退出码 2）。重试预算覆盖
并发原子计数不超支、预算耗尽后 `budget_exhausted` 截断与 `skipped_budget` 计数、
按失败码/target 的失败分布与库内明细对账，以及旧库（无 `attempt_details` 表）
生成报告不报错的降级路径。

```bash
python -m pytest
python -m pytest --cov=linkpatrol --cov-report=term-missing  # 若已安装 pytest-cov
```

核心模块 urlnorm/extractor/robots/checker/baseline 行覆盖目标 ≥90%，整体 ≥85%。
同一配置两次串行扫描的页面集合与断链集合一致。

## 已知限制

- 不执行 JavaScript，不渲染 SPA，不支持登录态页面。
- 不做定时调度守护进程；交给系统 cron 单次执行。
- 不做内容快照/变更告警、分布式抓取、消息队列、代理池或验证码处理。
- 不支持 ftp/file；`mailto`/`tel` 仅分类记录，`javascript:`/`data:` 等仅计数。
- 报告无交互图表、无外部前端资源；不做断链原因的 AI 推断与自动修复。
- sitemap 生成超过 50,000 URL 会拒绝；只含最终 2xx 的已爬 HTML 页。
