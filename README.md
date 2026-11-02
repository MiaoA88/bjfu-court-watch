# 北林羽毛球场「捡漏」监控

定时查询 [ehall 预约系统](https://ehall.bjfu.edu.cn/v2/reserve/hallView?id=54) 的场地余量，
发现目标日期/时段有空场就通过 WxPusher 推送到手机。**只查询、不下单**，收到通知后自己进系统预约。

- 扫描窗口：北京时间 **09:00–20:00**（系统 09:00–23:00 开放预约，23:00 后全部显示不可预约）
- 触发频率：GitHub Actions cron `*/5`（最快 5 分钟，实际常有 5–20 分钟延迟）
- 推送通道：WxPusher 极简推送（SPT），只需一个令牌

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

另外实测放场窗口约 **7 天**（当天起 7 天可约，第 8 天起 `status=5`），因此默认 `days_ahead: [0..7]`。

## 二、部署

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
`Request Headers` 里复制 `Cookie:` 整行的值。**只要 `vt=` 或 `vjvd=` 少一个就会 10042。**

### 3. 先跑连通性验证

`Actions → smoke → Run workflow`，看日志末尾「判定」：

| 结论 | 含义 |
|---|---|
| 网络可达，仅缺登录态（e=10042） | runner 能访问，配 Cookie 即可 |
| Cookie 有效，接口直接返回数据 | 全部就绪 |
| 返回的不是 JSON / 网络层失败 | 被 WAF 拦或需校内网络，见「换托管」 |

### 4. 本地自检（可选）

```bash
python -m src.cli probe              # 逐日拉余量，打印可约格数与状态分布
python -m src.cli watch --dry-run    # 只打印不推送、不写状态
```

## 三、日常运维

### Cookie 过期

CAS 会话通常数小时到几天。过期后会自动推送「⚠️ ehall 登录态失效」，重新取 Cookie 更新 Secret 即可。

### 改目标

只改 `config.yaml`：

```yaml
venue:
  resource_ids: [156]              # 156=羽毛球，157=乒乓球
  resource_name_include: ["1号"]    # 只盯 1 号场
targets:
  days_ahead: [1, 2, 3]
  time_windows: ["18:00-19:00", "19:00-20:00"]
matching:
  min_consecutive_slots: 2          # 必须连着两段都空
```

## 四、已知限制

1. **cron 不精确**：GitHub `schedule` 最短 5 分钟，高负载时延迟 5–20 分钟、偶有跳过。放场瞬间可能正好被延迟错过。
2. **Cookie 需手动续期**：统一身份认证有验证码，无法自动登录。
3. **公开仓库 60 天无提交会停用计划任务**：保持私有 + 偶尔提交。
4. **Cookie 可能绑定 IP**：若会话与 IP 绑定，跨网络复用会失效。`smoke` 的判定就是答案。
5. **接口改版**：前端带 `appSourceV` 版本号。届时跑 `probe` 看状态分布是否异常，再据新响应调整 `availability.py`。

### 若 Actions 不可用

代码与托管无关（读环境变量、写标准输出）。两种备选：境内云函数（腾讯云 SCF / 阿里云 FC）包一层入口即可；
或本机 Windows 计划任务（校园网 IP 最可信，但电脑要开着）。

## 五、目录

```
src/cli.py           入口：watch / probe / smoke
src/ehall.py         HTTP 客户端、信封解析、错误分类、重试
src/availability.py  list-page 解析：场地×时段网格 → Slot；含 probe 自检
src/matcher.py       可用性判定、时段匹配、连续区间合并、剔除已开始
src/state.py         跨运行去重状态
src/notify.py        WxPusher SPT 推送
src/timeutil.py      北京时间、扫描窗口、目标日期
tests/               72 个测试，不依赖网络
_probe/              早期抓取的前端资产与接口字典（溯源用，不参与运行）
```

## 六、合规

只读轮询、每次 8 个请求、5 分钟一次。学校《场馆预约管理办法》可能限制脚本访问账号，请自评风险；
勿外传 Cookie，勿用于代抢或牟利。
