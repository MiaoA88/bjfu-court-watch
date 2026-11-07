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
| `EHALL_COOKIE` | 登录预约系统后，从浏览器 Network 复制的完整 Cookie 请求头 |
| `EHALL_UA` | 同一浏览器的 User-Agent，建议随 Cookie 一起更新 |
| `WXPUSHER_SPT` | WxPusher 极简推送令牌 |

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
| `config-check.yml` | 配置或代码变更后校验，默认分支额外发配置公告，不提交文件 |
| `smoke.yml` | 手动验证 runner 连通性 |

工作流只申请仓库内容读取权限。监控使用 concurrency 串行运行；状态缓存键包含运行 ID 和重跑次数。不要让本地和另一个调度器同时写同一个状态文件。

## 去重与重试

记账单位是“场馆 + 日期 + 开始 + 结束 + 场地”，不是展示用的连续区间。

| 场景 | 行为 |
| --- | --- |
| 首次发现空场 | 通知一次 |
| 一直空闲且已成功通知 | 不重复通知 |
| 已报格子被占后重新释放 | 创建新的待通知机会 |
| 发送失败或缺少 SPT | 保留待通知，后续确认仍可约且符合规则时补发 |
| 待通知格子又被占用 | 撤销旧机会；再次释放时重新记录 |
| 连续区间缩短 | 不把边界变化当成新空场 |

`pending` 与观测状态 `occupied` 分开保存。只有真实推送通道返回成功，才移除待通知并记录 `notified`。旧版状态文件仍可读取，新字段默认初始化，无需删除旧状态。

消息发送和状态保存不能形成跨服务事务：发送成功后进程中断或缓存丢失，可能重复通知，不承诺严格“恰好一次”。

## 告警与健康状态

- **盯场表公告**：成功后才记录指纹，失败后重试，可关闭。独立 `announce` 不写监控指纹，可能与下次扫描公告重复。
- **每日启动**：当天首次符合执行路径的运行发送，可关闭；只证明任务执行过，不代表查询成功。窗口外可能发送启动说明，本周无目标时直接退出。
- **鉴权或缺 Cookie**：成功提醒后当天抑制重复；缺 SPT 或发送失败不记为已提醒，成功查询后解除旧告警状态。
- **连续查询失败**：达到 6 次尝试告警，失败后每隔至少 15 分钟重试；同一连续故障成功告警一次，成功扫描后重置。
- **长时间无成功扫描**：扫描窗口内至少 30 分钟无成功记录，下一次运行检查并提醒，重试间隔至少 15 分钟；正常夜间停扫不会在开窗瞬间触发。

**调度器完全停止时，程序不能唤醒自己告警。** 请在外部调度服务启用失败提醒，或让独立监控检查最近运行时间。新安装、缓存丢失时，没有足够历史推断此前是否中断。

```bash
python -m src.cli status
python -m src.cli -c config.yaml check --now 2026-11-04T15:00:00
python -m src.cli announce --dry-run
python -m src.cli probe --days 3
```

全局 `-c`、`-v` 放在子命令之前。`--now` 无时区时按北京时间解释，带偏移量时转换到北京时间。它只改变本地判断，不让接口返回历史快照。

`status` 只读取本机状态，不下载云端缓存。Actions 摘要和状态文件记录最近尝试/成功时间、已查日期数、格子数、命中数、通知结果、待通知数量与连续查询失败次数。`status=ok` 只表示查询成功；`notification=failed` 表示投递仍需处理，通知失败不增加查询失败计数。待通知数量可能包含当前规则不匹配的格子，不等于下一条消息数量。

| 现象 | 排查方向 |
| --- | --- |
| 没有运行记录 | 外部任务、PAT、分支、workflow 名及排队 |
| 运行但没查询 | 扫描窗口、本周剩余目标日期 |
| 登录态失效 | 重新登录并更新 Cookie，必要时同步 UA |
| `SchemaChanged` | 字段、结构、分页或日期异常，用 `probe` 检查 |
| 查询正常却无通知 | SPT、通知结果、是否已通知 |
| 缓存丢失后重复提醒 | 状态历史丢失，不等于去重失效 |

`check`、`announce` 校验或投递失败返回非零；空场通知失败也返回非零。已分类的接口和鉴权错误由 `watch` 记录并告警后返回 0，避免每轮失败邮件。不能仅凭 workflow 绿色判断业务健康。

## 接口边界

当前使用 `GET /site/reservation/list-page`，参数为 `hall_id`、`time`、`p=1`、`page_size`，读取 `list[].table`。默认仅状态 `1` 视为可预约。缺少 `status`、`margin`、场地名称、合法日期或网格结构会抛出 `SchemaChanged`；明确的空列表是合法空结果。

若 `total` 表明第一页不完整，会拒绝把部分数据当完整扫描，提示检查分页或增加 `page_size`。当前没有自动翻页。`probe` 保存首日原始业务响应到已忽略的 `probe-out/`，请勿随意公开原始数据。学校接口、访问限制和放场方式可能变化，以实际验证和学校规则为准。

项目不再把中文注释视为非法，工作流测试检查 YAML 结构、触发器和权限。

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
  dedup.py         观测变化和待通知机会
  state.py         状态兼容读取、原子写入和清理
  notify.py        WxPusher、日志及预览通道
  alerts.py        查询/鉴权告警与重试
  health.py        健康摘要与扫描中断提醒
  messages.py      通知正文、排序、扫描附录
tests/             离线测试与跨轮次故障回归
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
python -m compileall -q src tests
```

回归测试覆盖重新释放补发、通知指纹、告警冷却、接口异常、配置错误、跨午夜调度、旧状态兼容和失败时保存状态。测试数量以实际运行结果为准。
