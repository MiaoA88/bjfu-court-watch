# 北林羽毛球场「捡漏」监控

按**每周固定盯场表**查询 [ehall 预约系统](https://ehall.bjfu.edu.cn/v2/reserve/hallView?id=54) 的场地余量，
发现目标时段有空场就通过 WxPusher 推送到手机。**只查询、不下单**，收到通知后自己进系统预约。

- 盯场模型：`周几 + 该周几的时段`，按周重复（周一盯 20:00、周六盯 14:00 各管各的）
- 查询范围：**每天都跑**，只查本周（周一起算）还没过去的周几，周一一次覆盖全周
- 扫描窗口：北京时间 **14:00–22:00**（系统 09:00–23:00 开放预约）
- 推送通道：WxPusher 极简推送（SPT）

---

## 一、接口事实（实测，2026-11）

预约页是 Vue SPA，抓 HTML 只能拿到 `loading.gif`。真实数据在 JSON 接口里：

```
GET https://ehall.bjfu.edu.cn/site/reservation/list-page
    ?hall_id=54&time=2026-11-03&p=1&page_size=20
Cookie: PHPSESSID=...; vjuid=...; vjvd=...; vt=...
→ {"e":0,"d":{"total":2,"list":[{"id":156,"name":"羽毛球综合场馆（收费）",
      "table":{"595":[{"abscissa":"1号塑胶场地","yaxis":"18:00-19:00",
                       "row":{"status":1,"total":1,"margin":1},"date":"2026-11-03"}, …]}}]}}
```

要点：

- 参数是 **snake_case**（`hall_id` / `time`），驼峰会返回空数组。
- 鉴权只靠 Cookie，**不需要**模拟点击或浏览器；`e=10042` 即登录态失效。
- 场馆 54「多功能厅运动场地预约」下挂了两个项目：**羽毛球 156**、乒乓球 157。
  乒乓球几乎全天空闲，必须用 `venue.resource_ids: [156]` 收窄，否则会刷屏。

### status 语义（关键）

| status | margin | 含义 | 算不算空场 |
|---|---|---|---|
| 1 | 1 | 可预约 | ✅ |
| 3 | 0 | 已约满 | ❌ |
| 4 | 0 | 不可预约（当日已过 / 该日停用） | ❌ |
| 5 | 1 | **未开放**（未来日期还没放场） | ❌ |
| 0 | 0/1 | 已过期（当天已过去的时段） | ❌ |

**只看 `margin>0` 会把未来整月误报成空场**——未开放时 margin 同样返回 1。可用性必须判 `status==1`。

另外实测放场窗口约 **7 天**（当天起 7 天可约，第 8 天起 `status=5`）。

## 二、盯场表怎么配

改 [config.yaml](config.yaml) 的 `schedule`，`weekday` 用 ISO 编号（1=周一 … 7=周日），
日期和时段绑在一起，不同周几可以配不同时段：

```yaml
schedule:
  - weekday: 1                     # 周一
    time_windows: ["20:00-21:00"]
  - weekday: 6                     # 周六
    time_windows: ["14:00-22:00"]     # 一个窗口跨多个场次
```

**时段写法**（三种都支持）：

```yaml
time_windows: ["14:00-22:00"]                 # 单窗口，覆盖其间每个 1 小时场次
time_windows: ["14:00-16:00", "19:00-21:00"]  # 多窗口，跳开中间时段
time_windows: "14:00-22:00"                   # 只有一个窗口时可以省掉列表
```

匹配用的是**区间重叠**，不是字符串相等，所以 `14:00-22:00` 能命中 14:00-15:00、15:00-16:00……
每一个场次；不要求窗口边界与场次边界对齐。

**运行语义**：

- 系统**每周一早上**才放本周的场，所以只查**本周（周一起算）还没过去的周几**。
  周一运行会一次把本周所有配置的周几都查了；周中只查剩下的；过了就跳过。
- 多条规则落到同一天时合并时段、去重。
- 已经开始的场次会被剔除（今天的 19:00 场在 19:20 已经约不上了）。
- 同一空场（日期+时段+场地）只推一次；表按周循环，不会因为本周过去被清空，下周一自动重新生效。
- 本周已无目标日期时（例如周日只配了工作日），脚本直接退出，一次请求都不发。

## 三、改了配置怎么生效

1. 改 [config.yaml](config.yaml)；
2. `git add -A && git commit -m "..." && git push`；
3. 下一次运行（≤5 分钟）就会用新配置——workflow 每次都 `actions/checkout`，读的是仓库最新版本。

只改文件**不推送是不生效的**。

推送后第一次运行会发一条「⚙️ 盯场表已更新」，正文就是当前生效的完整时段表，
用来确认改动已经上线。这条只在表变化时发一次（靠状态里的指纹判断），不会每次刷。
不想要就设 `notify.announce_schedule_changes: false`。

## 四、部署

### 1. 建私有仓库并推送

```bash
git remote add origin git@github.com:<用户名>/<仓库名>.git
git push -u origin master
```

### 2. 配三个 Secret

`Settings → Secrets and variables → Actions`：

| 名称 | 内容 |
|---|---|
| `EHALL_COOKIE` | 完整 Cookie 请求头，形如 `route=…; PHPSESSID=…; vjuid=…; vjvd=…; vt=…` |
| `EHALL_UA` | 复制 Cookie 时**同一个浏览器**的 User-Agent |
| `WXPUSHER_SPT` | `SPT_` 开头的令牌，[扫码获取](https://wxpusher.zjiecode.com/docs/spt.html) |

取 Cookie：登录预约页 → F12 → Network → 刷新 → 点任意 `site/reservation/...` 请求 →
`Request Headers` 里复制 `Cookie:` 整行的值。**`vt=` 或 `vjvd=` 少一个就会 10042。**

### 3. 连通性验证

`Actions → smoke → Run workflow`，看日志末尾「判定」：

| 结论 | 含义 |
|---|---|
| 网络可达，仅缺登录态（e=10042） | runner 能访问，配 Cookie 即可 |
| Cookie 有效，接口直接返回数据 | 全部就绪 |
| 返回的不是 JSON / 网络层失败 | 被 WAF 拦或需校内网络，见「换托管」 |

### 4. 本地自检

```bash
python -m src.cli probe              # 逐日拉余量，打印可约格数与状态分布
python -m src.cli watch --dry-run    # 只打印不推送、不写状态
python -m src.cli watch --now 2026-11-04T15:00:00   # 伪造时间验证"周三查下周一"
```

## 五、日常运维

### Cookie 过期

CAS 会话通常数小时到几天。过期后会自动推送「⚠️ ehall 登录态失效」，重新取 Cookie 更新 Secret 即可。

### 场地名过滤

```yaml
venue:
  resource_ids: [156]              # 156=羽毛球，157=乒乓球
  resource_name_include: ["1号", "2号"]   # 只盯 1、2 号塑胶场地
matching:
  min_consecutive_slots: 2          # 必须连着两段都空
```

## 六、已知限制

1. **cron 不精确**：GitHub `schedule` 最短 5 分钟，高负载时延迟 5–20 分钟、偶有跳过。放场瞬间可能正好被延迟错过。
2. **Actions 额度**：私有仓库免费 2000 分钟/月。每天 96 次、单次约 20 秒 ≈ **960 分钟/月**，
   在额度内。若某月接近上限，把 cron 改成 `*/10` 即可减半。
3. **Cookie 需手动续期**：统一身份认证有验证码，无法自动登录。
4. **接口改版**：前端带 `appSourceV` 版本号。届时跑 `probe` 看状态分布是否异常，再据新响应调整 `availability.py`。

### ⚠️ workflow 文件里不要写中文

GitHub 对 `.github/workflows/*.yml` 的解析器**遇到中文注释会静默拒绝整个工作流**：
文件在仓库里、`git push` 成功，但 `Actions` 页面看不到它，`gh workflow list` 也不列，
且**不报任何错误**。本项目最初就是这样丢了 `watch.yml` 和 `smoke.yml`。
workflow 里的注释一律用英文，中文说明放 README；`test_workflow_comments_are_ascii_only` 会守住这条。

### 若 Actions 不可用

代码与托管无关（读环境变量、写标准输出）。两种备选：境内云函数（腾讯云 SCF / 阿里云 FC）包一层入口；
或本机 Windows 计划任务（校园网 IP 最可信，但电脑要开着）。

## 七、实测记录（2026-11-02）

- 本机与 **GitHub 托管 runner（境外 IP）** 都能直连 `ehall.bjfu.edu.cn`，未被 WAF 拦截。
- 同一份 Cookie 在 runner 上返回 `e=0`，说明**会话不绑定 IP**——这是本方案最大的不确定性，已排除。
- 单次运行 20–32 秒，资源过滤 2016 → 672 格，剔除已开始 672 → 600 格。
- 场馆 54 下羽毛球仅 6 块场地（1–6 号塑胶场地），乒乓球 12 块；羽毛球晚间时段较紧张，符合"捡漏"场景。

## 八、目录

```
src/cli.py           入口：watch / probe / smoke
src/schedule.py      盯场表：解析、映射到具体日期、推导 cron、指纹
src/ehall.py         HTTP 客户端、信封解析、错误分类、重试
src/availability.py  list-page 解析：场地×时段网格 → Slot；含 probe 自检
src/matcher.py       可用性判定、时段匹配、连续区间合并、剔除已开始
src/state.py         跨运行去重状态、盯场表指纹
src/notify.py        WxPusher SPT 推送
src/timeutil.py      北京时间、扫描窗口
tests/               114 个测试，不依赖网络
_probe/              早期抓取的前端资产与接口字典（溯源用，不参与运行）
```

## 九、合规

只读轮询，每次 1 个请求。学校《场馆预约管理办法》可能限制脚本访问账号，请自评风险；
勿外传 Cookie，勿用于代抢或牟利。
