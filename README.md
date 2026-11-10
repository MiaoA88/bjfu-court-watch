# 北林羽毛球场余量监控

按每周盯场表查询预约系统，发现符合条件的空场后，通过 WxPusher SPT 提醒。只查询、发送通知，不提交预约；收到消息后需要自行预约。

## 当前行为

- 使用 `Asia/Shanghai` 时区，默认扫描窗口为 **10:00–23:00，左闭右开**。
- 外部调度每 5 分钟启动一次，每轮完成后退出，不是常驻进程。
- 只查询**本周尚未过去的目标日期**，周一起算，不查下周。这是项目策略，不是对学校放场规则的保证。
- 默认查询场馆 `54` 中的羽毛球资源 `156`，支持场地名称筛选。
- 可预约条件：`status == available_status`（默认 `1`）、`margin > 0`、场次尚未开始。
- 时间窗口按区间重叠匹配，同场地相邻场次合并展示；`min_consecutive_slots` 是连续场次数，不是小时数。

当前配置理论上每天触发 156 次，31 天最多 4836 次，不含手动运行。每轮逐日查询多个日期，另有资源查询和失败重试，**一次运行不等于一个 HTTP 请求**。费用请按仓库方案、runner 类型和作业时长核对，本文不承诺固定免费额度。

## 快速开始

Python 3.10 及以上；CI 使用 Python 3.12。从项目根目录执行：

```bash
python -m venv .venv
# PowerShell：.venv\Scripts\Activate.ps1
# Linux/macOS：source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m src.cli check
python -m pytest -q
```

生产环境只安装 `requirements.txt`。测试不访问真实预约接口、不发送通知。

在当前 shell 或 GitHub Actions Secrets 中配置：

| 环境变量 | 用途 |
| --- | --- |
| `EHALL_COOKIE` | 登录后从浏览器 Network 复制完整 Cookie 值，例如 `route=xxx; PHPSESSID=xxx`；保留实际请求中的其他 Cookie 字段 |
| `EHALL_UA` | 同一浏览器完整的 User-Agent 值，例如 `Mozilla/5.0 ...`；建议随 Cookie 一起更新 |
| `WXPUSHER_SPT` | 一个或多个 WxPusher 极简推送令牌，多个用户用英文分号 `;` 分隔，例如 `SPT-xxx;SPT-yyy;SPT-zzz`（令牌以平台实际发放值为准） |

三个 Secret 均直接填写值，不带 `Cookie:`、`User-Agent:` 等请求头名称，不加外围引号或换行。示例中的 `xxx`、`...` 需要替换成实际值；Cookie 字段间使用 `; ` 分隔，不要只保留示例里的两个字段。

多用户会逐个发送同一条消息，自动去除令牌两端空格、空项和重复项；某个用户发送失败不影响继续尝试其他用户。全部成功后才记为已通知，部分失败时下一轮可能向已收到的用户重复发送，不另增每用户状态。

程序不自动加载 `.env`。不要把密钥写进源码、提交记录或公开日志。

```bash
python -m src.cli smoke
python -m src.cli watch --dry-run
python -m src.cli watch
```

`watch --dry-run` **仍会访问真实接口，需要 Cookie**，但不推送、不写状态。离线验证请用测试、`check` 或 `announce --dry-run`。

## 配置

实际配置为 `config.yaml`，以下是字段示例：

```yaml
venue:
  hall_id: 54
  page_size: 20
  available_status: 1
  resource_ids: [156]
  resource_name_include: []
  resource_name_exclude: []
schedule:
  - weekday: 1
    time_windows: ["19:00-22:00"]
  - weekday: 6
    time_windows: ["14:00-16:00", "19:00-21:00"]
matching:
  min_consecutive_slots: 1
active_hours: ["10:00", "23:00"]
scan_interval_minutes: 5
notify:
  provider: wxpusher_spt
  detail_url: "https://ehall.bjfu.edu.cn/v2/reserve/hallView?id=54"
  announce_schedule_changes: true
  announce_start: true
http:
  timeout_seconds: 15
  retries: 2
state:
  path: .state/notified.json
```

- `weekday`：1=周一，7=周日；单窗口可直接写字符串，也可写多个窗口。
- `active_hours`：两个不同的整点，支持跨午夜，如 `22:00–06:00`。非整点配置会被拒绝，避免生成不准确的 cron。
- `scan_interval_minutes`：1–59 的整数。`*/N` 每小时重新计数，N 不整除 60 时跨小时的间隔不均匀。脚本本身不循环。
- 场地 include/exclude：非空字符串列表，空列表不限制。
- `http.retries`：首次请求之外的重试次数；超时必须为有限正数。
- `state.path`：相对当前工作目录。云端 workflow 缓存 `.state`，更改路径时需同步修改缓存路径和保存条件。

`watch`、`check`、`announce`、`probe` 共用配置校验；非法类型、通知通道、时间窗口和数值范围提前报错。`smoke` 独立验证连通性，不读取配置。

修改后运行 `python -m src.cli check`，查看目标日期和建议 cron。它**不连接、不读取、不自动更新 cron-job.org**。更改窗口或间隔时，必须同步调整外部任务。

## 云端部署

1. 将项目推送到自己的 GitHub 仓库，启用 Actions。
2. 配置上述三个仓库 Secret。
3. 手动运行 `smoke`，再运行 `court-watch`，确认扫描摘要和推送。
4. 在 cron-job.org 配置任务，替换仓库、分支、令牌占位符：

```text
URL: https://api.github.com/repos/<owner>/<repo>/actions/workflows/watch.yml/dispatches
Method: POST
Body: {"ref":"<default-branch>"}
Timezone: Asia/Shanghai
Schedule: */5 10-22 * * *

Accept: application/vnd.github+json
Authorization: Bearer <PAT>
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

PAT 需有目标仓库访问权限和触发 Actions 的权限。按 GitHub 当前设置选择最小权限，检查有效期、外部任务启用状态及执行记录。触发请求被接受不代表扫描成功，仍需查看 workflow 结果。

| 工作流 | 职责 |
| --- | --- |
| `watch.yml` | 手动或外部触发扫描，恢复状态，扫描失败后仍尝试保存状态 |
| `tests.yml` | push / PR 时运行离线测试和配置自检 |
| `config-check.yml` | 配置或代码变更后只做校验，不推送公告、不提交文件 |
| `smoke.yml` | 手动验证 runner 连通性 |

工作流只申请仓库内容读取权限。监控使用 concurrency 串行运行；状态缓存键包含运行 ID 和重跑次数。不要让本地和另一个调度器同时写同一个状态文件。

## 扫描与推送

空场去重单位是“场馆 + 日期 + 开始 + 结束 + 场地”。完整查询本轮所有目标日期后才更新观测，某一天查询失败不会把其他日期的场地误判为被占用。

| 场景 | 行为 |
| --- | --- |
| 首次发现符合盯场规则的空场 | 推送一次 |
| 同一格子持续空闲且已成功推送 | 不重复推送 |
| 观察到格子被占用 | 清除该格子的已通知记录 |
| 被占后再次释放 | 重新推送 |
| 发送失败或缺少 SPT | 不记为已通知；下一轮仍可约且符合规则时补发 |
| 格子从接口结果中缺失或查询失败 | 不推断被占用，不清除已通知记录 |

每个场地、每个时段独立判断。连续场次要求基于完整空场判断，通过筛选后仅推送未通知的格子，相邻时段可合并展示。展示区间缩短不会触发重复通知。

- 单个空场标题：`🏸 周三的19:00-20:00有空场`。
- 多个空场标题：`🏸 周三的19:00-20:00等有多个空场`。
- 按本次待推送的独立格子计数，同一时段两块场地也算多个；标题取日期、开始时间、场地排序后的第一个格子。正文列出本次扫描符合盯场规则的全部空场，按日期、时段分组，并将本轮新增的场地加粗。标题只按本轮待提醒的新增格子计数：即使已有多个旧空场，仅新增一个时也只写“有空场”；首次发现及发送失败后的补发仍按新增待提醒处理。

正文附简短扫描记录：今日实际扫描次数（含失败尝试）、今日剩余计划扫描次数、扫描窗口及间隔、查询日期与检查格子数、当前及新增空场数、扫描耗时。剩余次数按配置的 cron 计划估算，不保证调度器实际执行；不计当前分钟，跨午夜窗口只计算今日剩余部分。实际次数随状态缓存保存，每天重置，窗口外及无目标日期不计；新版本上线当天从启用计数后起算，缓存丢失会重新计数，不保留逐轮历史。

仅保留以下自动提醒：

- **每日启动**：当天首次执行 `watch` 时推送一次，即使在扫描窗口外、本周已无目标或随后查询失败也会尝试发送；可用 `announce_start` 关闭。
- **登录失效或缺 Cookie**：成功提醒后持续抑制，不因跨天重复；完整查询成功后解除抑制，下一次失效再提醒。
- **盯场表变化**：首次建立记录或表有变化时公告，投递成功后保存指纹；可用 `announce_schedule_changes` 关闭。自动公告统一由 `watch` 触发，配置校验不发公告。

这些提醒发送失败时不记为已发送，下轮再尝试。不额外设置连续失败告警、扫描中断告警、冷却计时或待通知队列。普通接口错误只写运行日志。启动通知只说明任务执行过，不代表查询成功。

状态只保存已成功通知的格子、登录失效提醒标记、盯场表指纹、每日启动日期及当天扫描计数；继续使用原子写入及 Actions 缓存。旧状态自动读取，其中占用、待通知格子会清除对应已通知记录，无需手动删除状态文件。

消息发送和状态保存无法形成跨服务事务；发送后进程中断或缓存丢失，可能重复通知。

## 排查与手动命令

```bash
python -m src.cli -c config.yaml check --now 2026-11-04T15:00:00
python -m src.cli announce --dry-run
python -m src.cli probe --days 3
```

全局 `-c`、`-v` 放在子命令之前。`--now` 无时区时按北京时间解释，带偏移量时转换到北京时间；它只改变本地判断，不让接口返回历史快照。

`announce` 是手动推送/预览入口，不写监控指纹；如主动执行真实推送，下次 `watch` 仍可能发送自动公告。`watch --dry-run` 不推送、不写状态。原 `status` 命令已移除，扫描结果直接查看运行日志，Actions 摘要只记录简短运行时间与窗口信息。

| 现象 | 排查方向 |
| --- | --- |
| 没有运行记录 | 外部任务、PAT、分支、workflow 名及排队 |
| 运行但没查询 | 扫描窗口、本周剩余目标日期 |
| 登录态失效 | 重新登录并更新 Cookie，必要时同步 UA |
| `SchemaChanged` | 字段、结构、分页或日期异常，用 `probe` 检查 |
| 查询正常却无通知 | SPT、通知结果、是否已通知 |
| 缓存丢失后重复提醒 | 状态历史丢失 |

`check`、`announce` 校验或投递失败返回非零；空场通知失败也返回非零。已分类的接口和鉴权错误由 `watch` 记日志后返回 0，避免每轮失败邮件；不能仅凭 workflow 绿色判断查询成功。

## 接口边界

当前使用 `GET /site/reservation/list-page`，参数为 `hall_id`、`time`、`p=1`、`page_size`，读取 `list[].table`。默认仅状态 `1` 视为可预约。缺少 `status`、`margin`、场地名称、合法日期或网格结构会抛出 `SchemaChanged`；明确的空列表是合法空结果。

若 `total` 表明第一页不完整，会拒绝把部分数据当完整扫描，提示检查分页或增加 `page_size`。当前没有自动翻页。`probe` 保存首日原始业务响应到已忽略的 `probe-out/`，请勿随意公开原始数据。学校接口、访问限制和放场方式可能变化，以实际验证和学校规则为准。

## 代码结构与开发

```text
src/
  cli.py           参数、命令和扫描编排
  config.py        统一配置读取与校验
  schedule.py      周计划、日期、cron、运行规模
  timeutil.py      北京时间与窗口工具
  ehall.py         HTTP、重试、信封与错误分类
  availability.py  接口结构校验与场次解析
  matcher.py       场地筛选、重叠与连续区间
  dedup.py         按格子去重、筛出待推送空场
  state.py         最小通知状态、旧状态读取与原子写入
  notify.py        WxPusher、日志及预览通道
  messages.py      通知标题、正文与排序
tests/             离线测试与关键跨轮次回归
.github/workflows/ CI 与云端监控
config.yaml        实际盯场配置
requirements.txt   运行依赖
requirements-dev.txt  测试依赖
```

`.state/`、`probe-out/`、`_probe/`、`local-deploy/` 是本地状态、诊断产物或个人部署辅助文件，不是仓库安装前提。保留 `python -m src.cli` 入口，不要求修改已有启动命令。

修改实现后执行：

```bash
python -m pytest -q
python -m src.cli check
```

回归测试重点覆盖格子独立去重、重新释放与失败补发、消息标题、启动及登录提醒、盯场表公告，并保留接口解析、部分查询失败、旧状态读取和 dry-run 验证。

## 致谢

感谢GPT and DeepSeek。