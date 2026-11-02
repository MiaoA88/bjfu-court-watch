# 北林羽毛球场「捡漏」监控

定时查询 [ehall 预约系统](https://ehall.bjfu.edu.cn/v2/reserve/hallView?id=54) 的场地余量，
发现目标日期/时段有空场就通过 WxPusher 推送到手机。**只查询、不下单**，收到通知后自己进系统预约。

- 查询窗口：北京时间 **09:00–20:00**（系统 20:00–次日 09:00 关闭，此间不查询、不推送）
- 触发频率：GitHub Actions cron `*/5`（最快 5 分钟，实际常有 5–20 分钟延迟）
- 推送通道：WxPusher 极简推送（SPT），只需一个令牌

---

## 一、原理（为什么不是抓网页）

预约页是 Vue SPA，直接抓 HTML 只能拿到一个 `loading.gif`。真正的数据在 JSON 接口里：

```
https://ehall.bjfu.edu.cn/site/reservation/...
返回统一信封 {"e": 业务码, "m": 文案, "d": 数据}
未登录时 e = "10042"（该操作需要登录，请登录后重试）
```

所以本程序直接调接口：**鉴权只靠 Cookie**，不需要模拟点击，也不需要浏览器。

## 二、部署（约 10 分钟）

### 1. 建仓库

GitHub 新建**私有**仓库（务必私有），把本目录推上去：

```bash
git remote add origin git@github.com:<你的用户名>/<仓库名>.git
git push -u origin master
```

### 2. 配三个 Secret

`Settings → Secrets and variables → Actions → New repository secret`：

| 名称 | 内容 |
|---|---|
| `EHALL_COOKIE` | 登录后从浏览器复制的完整 Cookie 字符串 |
| `EHALL_UA` | 复制 Cookie 时**同一个浏览器**的 User-Agent |
| `WXPUSHER_SPT` | `SPT_` 开头的令牌，[扫码获取](https://wxpusher.zjiecode.com/docs/spt.html) |

**取 Cookie 的步骤**：浏览器登录预约页 → F12 → Network → 点开任一个 `site/reservation/...` 请求 →
`Request Headers` 里复制 `Cookie:` 整行的值；同一面板里复制 `User-Agent:`。

### 3. 先跑连通性验证

`Actions → smoke → Run workflow`。看日志末尾的「判定」：

| 结论 | 含义 | 下一步 |
|---|---|---|
| 网络可达，仅缺登录态（e=10042） | runner 能访问，就差 Cookie | 配好 Cookie 继续 |
| Cookie 有效，接口直接返回数据 | 全部就绪 | 继续 |
| 返回的不是 JSON 信封 | 被 WAF 拦 / 需要校内网络 | 见「换托管」 |
| 网络层失败 | 同上 | 见「换托管」 |

### 4. 跑一次接口探测，定型字段

本地执行（需要 Python 3.12+，`pip install -r requirements.txt`）：

```bash
set EHALL_COOKIE=<你的 Cookie>
set EHALL_UA=<同一个 UA>
python -m src.cli probe --date 2026-11-05
```

它会遍历候选端点，打印每个组合的 `http / code / slots`，把响应落到 `probe-out/`，
并在最后给出「建议写入 config.yaml」的两行。把这行填进 `config.yaml` 的 `venue.margin_endpoint` 即可。

若 `slots=0` 但接口确实有数据，说明字段名不同：看 `probe-out/*.json`，把真实字段名补进
`config.yaml` 的 `fields` 段（**改配置，不改代码**）。

### 5. 试跑

```bash
python -m src.cli watch --dry-run   # 只打印不推送、不写状态
python -m src.cli watch             # 真推送
```

确认无误后 `Actions → court-watch → Run workflow` 手动跑一次。

## 三、日常运维

### Cookie 过期

CAS 会话通常只有数小时到几天，过期后监控会自动推送「⚠️ ehall 登录态失效」。
重新取 Cookie（步骤 2）并更新 Secret 即可。

### 改目标

只改 `config.yaml`，提交后自动生效：

```yaml
targets:
  days_ahead: [1, 2]                 # 明天、后天
  dates: ["2026-11-08"]              # 也可以写死日期
  time_windows: ["18:00-19:00"]      # 想盯的时段
venue:
  resource_name_include: ["7号"]     # 只关注 7 号场
matching:
  min_consecutive_slots: 2           # 必须连着两段都空才推
```

## 四、已知限制

1. **cron 不精确**：GitHub 的 `schedule` 最短 5 分钟，且高负载时段会延迟 5–20 分钟、偶有跳过。
   放场瞬间可能正好被延迟错过——这是平台限制，不是代码问题。
2. **Cookie 需手动续期**：统一身份认证有验证码，无法自动登录。
3. **公开仓库 60 天无提交会停用计划任务**：保持私有仓库 + 偶尔提交即可。
4. **Cookie 可能绑定 IP**：若学校会话与 IP 绑定，跨网络（含 GitHub 的境外 IP）复用会失效。
   `smoke` 的判定结果就是答案。
5. **接口可能改版**：前端带 `appSourceV` 版本号，改版后字段可能变。届时重跑 `probe` 并按提示改 `fields`。

### 若 Actions 不可用

代码本身与托管无关（读环境变量 + 标准输出）。两种备选：

- **境内云函数**（腾讯云 SCF / 阿里云 FC）：把 `python -m src.cli watch` 包成入口函数，配定时触发器。
- **本机 Windows 计划任务**：校园网 IP 最可信，缺点是电脑要开着。

## 五、目录

```
src/cli.py           入口：watch / probe / smoke
src/ehall.py         HTTP 客户端、信封解析、错误分类、重试
src/availability.py  接口适配：把任意响应抽成 Slot；含 probe
src/matcher.py       时段匹配、连续区间合并
src/state.py         跨运行去重状态
src/notify.py        WxPusher SPT 推送
src/timeutil.py      北京时间、查询窗口、目标日期
tests/               65 个测试，不依赖网络
_probe/              上一轮抓取的前端资产与接口字典（溯源用，不参与运行）
```

## 六、本地开发

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

## 七、合规

只读轮询、每次 3–5 个请求、5 分钟一次，对系统负载的影响可忽略。但学校《场馆预约管理办法》
可能限制脚本访问账号，请自行评估风险。请勿外传 Cookie，勿用于代抢或牟利。
