# AI Studio Playground 协议分析

**日期** 2026-10-16 ｜ 口径：把 **aistudio.google.com 的 Playground（网页对话/生成页）** 反代成 OpenAI/Gemini 兼容 API 的项目族
**数据来源**：GitHub API 快照（星数/最后推送）+ 各仓库 README/官方 docs/源码原文（本地留存见 `raw/`、`src/`）
**边界**：本报告不覆盖 Gemini 网页版（gemini.google.com）反代、Build App 反代、API key 池 —— 三者在 §5 单列对照

---

## 0.5 入口与额度自查（官方出处）

| 页面 | URL | 官方出处 |
|---|---|---|
| **Playground**（本报告主角） | `https://aistudio.google.com/` —— 打开即默认落在 Playground 新对话页；深层路径 `/app/prompts/new_chat`（旧路径 `/prompts/new_chat` 仍在） | 官方 quickstart 原文：「Open Google AI Studio. The **Playground** will be open by default with a new chat prompt」 |
| **Build mode** | `https://aistudio.google.com/apps` | 官方文档：「Start vibe coding in Google AI Studio's [Build mode]」 |
| **查自己当前额度** | `https://aistudio.google.com/rate-limit?timeRange=last-28-days` | 官方 rate-limits 页 |
| **查自己的套餐/项目层级** | `https://aistudio.google.com/projects` | 官方 rate-limits 页 |

实测（2026-10-16，本机 curl）：`/app/prompts/new_chat`、`/prompts/new_chat`、`/apps`、`/prompts` 未登录一律 302 → `accounts.google.com/ServiceLogin`（四个入口都存在、都要求登录）；`aistudio.google.com/` 302 → `/welcome`。RPC 主机 `alkalimakersuite-pa.clients6.google.com` 在 AI Studio 页面 HTML 的 link 标签里直接可见 —— Playground 的工程名就是 **MakerSuite**（旧称）。

---

## 0.6 Playground ≠「创建 API 的地方」

三个页面、两套后端、两套身份：

| 页面 | URL | 用途 | 后端 | 身份载体 |
|---|---|---|---|---|
| **Playground** | `aistudio.google.com/`（`/app/prompts/new_chat`） | 交互式聊天 / 试 prompt / 生图 / 调参 | `alkalimakersuite-pa.clients6.google.com` 的 `GenerateContent`（gRPC-web） | 账号 Cookie + BotGuard proof —— **不需要 API key** |
| **Build mode** | `aistudio.google.com/apps` | vibe coding 建应用 | Build 应用经宿主页代理 `ProxyUnaryCall` / `ProxyStreamedCall` | 同上（也可填入自己的 key，那就改走 key 配额） |
| **API keys** | `aistudio.google.com/apikey`（另有 `/api-keys`） | 创建 / 查看 API key | 产出 `AIza…` | Google 账号，产出物用于下一行 |
| **调用模型** | `generativelanguage.googleapis.com/v1beta/models/…` | 正式 API | REST / gRPC | **API key** |

实测（2026-10-16）：

```
GET https://generativelanguage.googleapis.com/v1beta/models        （不带 key）
→ 403 PERMISSION_DENIED
  "Method doesn't allow unregistered callers (callers without established
   identity). Please use API Key or other form of API consumer identity to
   call this API."

GET https://alkalimakersuite-pa.clients6.google.com/$rpc/…MakerSuiteService/GenerateContent
→ 302（未带页面 Cookie，重定向到登录）—— 不吃 API key
```

这两条 curl 就是 Playground 反代能成立的全部理由：**它走的是账号鉴权的那条后端；API key 那条后端连门都不给它开。**

历史注脚：AI Studio 的前身叫 **MakerSuite**，Playground 试玩页与 API key 页从那时起就是两块不同的东西 —— RPC 主机名 `alkalimakersuite-pa` 至今没改。

2026 年的 API key 新规（官方 `docs/api-key`）：**自 2026-05-28 起，AI Studio 新建的 key 一律是 auth key**；**未加限制的标准 key（unrestricted standard key）会被 Gemini API 直接拒绝**。

---

## 1. 结论先行

Playground 反代是**四条 AI Studio 额度通道里唯一一条"用页面身份换额度"的通道**：

| 通道 | 身份载体 | 端点 | 额度池 |
|---|---|---|---|
| **Playground**（本报告） | Google 账号 cookie + BotGuard/WAA proof | `alkalimakersuite-pa.clients6.google.com` 的 `GenerateContent` RPC | 与 Build **分开计算** |
| Build App | 同上，经宿主页代理 RPC | `ProxyStreamedCall` / `ProxyUnaryCall` | 独立 |
| API key | AI Studio 签发的 `AIza…` key | `generativelanguage.googleapis.com` | 独立（免费层最低） |
| Gemini CLI / Code Assist | CLI OAuth 凭证 | Cloud Code Assist | 独立（聚星现用这条） |

四个池子互不相通 —— 这是 Playground 反代对**聚星**的全部价值：多一条额度来源，且这条路用的是"真人网页行为"最强的一种身份（不是 API key、不是 CLI 凭证）。

现状三句话：

1. **技术上已经完全跑通**，而且从"点页面按钮"进化到了"只借页面的 Attestation，请求自己发"（`WAA_BACKEND=go`，纯 Go，不启动浏览器）。
2. **活跃度断层明显**：星数最高那个（2511★）已停更 6 个月；今天还在推代码的是 Go 系的 Mag1cFall（266★）。
3. **许可证风险不对称**：旗舰项目是 **AGPLv3**，两家 Go 系是 **MIT**。商业站选型先看这一条。

---

## 2. 项目谱系（按技术路线分三代）

| 代 | 路线 | 代表 | 星 | 语言 | 最后推送 | 许可 |
|---|---|---|---|---|---|---|
| **Ⅰ UI 自动化** | Camoufox/Playwright 真的去点 Playground 页面 | **CJackHwang/AIstudioProxyAPI** | 2511 | Python | 2026-04-22 | **AGPLv3** |
| | | globlord/AIstudioProxyAPI | 54 | Node | 2026-10-12 | — |
| | | Lianues/AIStudioBridge | 220 | JS | 2025-07-08 | — |
| | | gaoao-3/aistudio-web-api | 8 | TS+Vue | 2026-10-14 | — |
| **Ⅱ 协议级 + proof** | 拦一次页面请求拿 BotGuard snapshot，之后自己构造 wire body 直发 | **chrysoljq/aistudio-api** | 219 | Python | 2026-07-31 | MIT |
| **Ⅲ 双通道 Go（终点形态）** | Playground + Build 双通道，proof 在进程内跑，**零浏览器** | **Mag1cFall/AIStudio2API** | 266 | Go | **2026-10-16** | MIT |
| | | saubaka/AIstudio-API-Next（fork） | 0 | Go | 2026-10-05 | MIT |

三代的分水岭是**浏览器的位置**：

- Ⅰ 代：浏览器在**关键路径**上 —— 每个请求都要过页面 DOM，慢、脆、怕改版。
- Ⅱ 代：浏览器只在**取 snapshot/cookie** 时用一次（chrysoljq 的 `SnapshotExtractor`），snapshot 缓存 3600s，业务请求走自己写的 wire codec。
- Ⅲ 代：浏览器**完全退出请求链** —— 纯 Go 后端在进程内跑 BotGuard 的 interpreter/program，按账户指纹模拟 Firefox 152 的网络形状直接发请求；浏览器只在"首次登录"时按需拉起。

---

## 3. 逐项目档案

### 3.1 Mag1cFall/AIStudio2API — 266★ · Go · MIT · **今日仍在推**

**唯一一个把 Playground 与 Build 做成可开关双通道的项目。**

```bash
UPSTREAM_CHANNELS=playground,build     # 默认两条都开；一条撞限额后由另一条继续
ROUTING_STRATEGY=round-robin           # fill-first = 账号粘性优先（填满一个再下一个）
WAA_BACKEND=go                         # 进程内 BotGuard，不下载、不启动 Camoufox
PER_ACCOUNT_CONCURRENCY=2              # 单账号并发槽位
WARM_WORKER_LIMIT=5                    # 常驻预热账户数（小机器要调低）
MAX_ACTIVE_WORKERS=10                  # 峰值 Worker 上限
```

- **端口** `2048`；管理控制台带登录（`ADMIN_AUTH_ENABLED`，会话 12h）；公开 API 走 `PROXY_API_KEY`。
- **账户目录**：`auth/<邮箱>/account.json`（邮箱/代理/时区）、`storage-state.json`（cookie + 续签材料）、`runtime-state.json`（权益等级/模型资格/冷却）、`camoufox-cache/`。
- **认证续签**：Chrome 导入的账户保留 **DBSC** 续签材料，**续签动作在 Windows 上执行** —— 服务器部署要先想清楚这条（Linux/Docker 里 Chrome 导入账户失效后需回 Windows 重导）。
- **协议覆盖**是这批项目里最宽的：OpenAI Chat/Responses（含 `/v1/responses/compact` 上下文压缩）、Anthropic Messages、Gemini GenerateContent、Files、Embeddings、Images（Nano Banana）、Audio/TTS、Videos（Veo）、Live/Robotics（WebSocket）。
- **文档即资产**：`docs/protocol.md`（126 KB，全 wire 字段）、`docs/waa.md`（62 KB，WAA 全链路）、`docs/reverse-engineering.md`（17 KB，**可复用逆向开发指南**）。

### 3.2 chrysoljq/aistudio-api — 219★ · Python · MIT · 2026-07-31

**纯 Playground 反代里源码最干净的一个**（90 个文件 / 3.8 MB，解包后我把网关、浏览器、账户三层读完了）。链路：

```
客户端 → FastAPI(:8080) → Wire Codec + BotGuard → Camoufox(无头) XHR hook → AI Studio
```

关键实现（有源码行号可查）：

| 事实 | 位置 |
|---|---|
| 目标 RPC 主机 `alkalimakersuite-pa.clients6.google.com` | `infrastructure/browser/snapshot_extractor.py:21`、`infrastructure/browser/proxy.py:34` |
| 打开 `https://aistudio.google.com/app/prompts/new_chat`，`page.route("**/alkalimakersuite-pa.clients6.google.com/**")` 拦 POST，取 **body[4]** 里 `!` 开头的 snapshot | `snapshot_extractor.py:53-68` |
| 另有一个本地 MITM 代理 + `GrpcDumpHandler`（只认 URL 含 `GenerateContent` 的请求） | `proxy.py:33-186` |
| 多账号轮询：`round_robin` / `lru` / `least_rl`（按被限流次数最少选） | `application/account_rotator.py` |
| 会员档识别（Free / Pro / Ultra） | `infrastructure/account/tier_detector.py` |
| BotGuard 用**特征匹配**定位 snapshot 函数（`.snapshot({` + `content` + `yield`），函数名随 bundle 变（Mv→Ov→Sv），特征不变 | README「BotGuard 原理」 |

- **额度/订阅**：免费档默认文本模型是 `gemma-4-31b-it`；**生图模型（Nano Banana 系列）仅 Pro/Ultra** 可用。
- **冷却**：`AISTUDIO_ACCOUNT_COOLDOWN_SECONDS=60`（限流后冷却）。
- 部署：`python3 main.py login`（无头交互登录，支持手机确认/安全码/验证器）→ `main.py server --port 8080`；也有 `ghcr.io/chrysoljq/aistudio-api:latest`。
- **唯一让我犹豫的点**：`main.py server` 与 `login` 之外，UI 只有半成品（README TODO 里写着「完整 webui 支持」未完成）；且已 2.5 个月未推代码。

### 3.3 CJackHwang/AIstudioProxyAPI — 2511★ · Python · **AGPLv3** · 2026-04-22（停更 6 个月）

这条赛道星数最高、社区最大（多名贡献者、Linux.do 背书、多实例 Docker 管理器、函数调用三模式 `auto/native/emulated`、认证 profile 自动轮转 + cookie 周期刷新、桌面 GUI 启动器）。

**但它是 Ⅰ 代路线**：Camoufox + Playwright 驱动真实页面，`launch_camoufox.py --headless`。所以：
- 每个请求都过页面 → 吞吐低、改版即断；
- 端口 `2048`、流式单独 `STREAM_PORT=3120`；
- **AGPLv3**：对商业站是硬约束（对外提供网络服务须向使用者提供源码）。

停更 6 个月不等于死 —— fork 生态（MasuRii 等下游分支）仍在动，它现在的价值更多是"参考实现 + 社区文档"。

### 3.4 saubaka/AIstudio-API-Next — 0★ · Go · MIT · 2026-10-05

Mag1cFall 的 fork，做了一件有意义的事：**把 Playground 与 Build 拆成两个固定入口，禁止自动跨渠道回退**：

```
Playground:  http://127.0.0.1:2048/playground/v1     (Anthropic/Gemini 用 /playground)
Build:       http://127.0.0.1:2048/build/v1          (Anthropic/Gemini 用 /build)
```

理由是"固定渠道请求不会因额度不足而偷偷切换到另一渠道"。对**按渠道计费/按渠道控量**的转售站，这个语义比上游的自动回退更好用。星数 0 只说明它还新（10-05 建仓后仍在推），不代表质量差 —— 它带着上游完整文档与 `渠道重构说明.md`。

### 3.5 其余（略档）

| 项目 | 星 | 说明 |
|---|---|---|
| globlord/AIstudioProxyAPI | 54 | Node + Playwright，2026-10-12 有更新，轻量 |
| gaoao-3/aistudio-web-api | 8 | TS + Vue，主打多账号轮换 + 自动故障切换 |
| Lianues/AIStudioBridge | 220 | 「对话页面转发为 API」，2025-07 停更 |
| yaron4u/AIstudioProxyAPIClient | 5 | FastAPI + Playwright + Camoufox 的客户端 |

---

## 4. 技术事实清单（有出处，可直接用于自研）

### 4.1 受保护 RPC 与 proof 字段（`docs/waa.md:34-44`）

| RPC | proof 写入 field | binding（SHA-256 前的原文） | 归属通道 |
|---|---|---|---|
| `GenerateContent` | **5** | `contents` 各 part 以空格连接（text 取原文、inline data 取标准 Base64、Drive file 取 file ID，其余取空串） | **Playground** |
| `ProxyStreamedCall` / `ProxyUnaryCall` | **3** | `<路径> <请求体>` | **Build** |
| `GenerateVideo` | 8 | 视频提示词 | MakerSuite |
| Bidi setup / text / tool response | 6 | 见原文 | WebChannel |

### 4.2 WAA（Web Application Attestation）= BotGuard proof

- 每个受保护请求都要一份 `!` 开头的 proof；服务端持有一份 **BotGuard VM** 按请求生成。
- `camoufox` 后端：proof 由页面的 `default_MakerSuite` 命名空间里 `.snapshot({`+`content` 的高层函数生成；**bootstrap 请求在发出前被 `network.failRequest` 终止，不消耗生成额度**（`docs/waa.md:64-68`）。
- `go` 后端：goja VM 在进程内跑官方 interpreter + program，宿主按 **Firefox 152** 的网络形状补齐（连 `Intl`、正则字面量解析时机、`RegExp.prototype` 的 Symbol 键顺序都对齐了 —— 说明上游确实在看这些指纹）。
- 工具链源头：[LuanRT/BgUtils](https://github.com/LuanRT/BgUtils)（chrysoljq 的致谢项）。

### 4.3 额度与权益

**Playground 与 Build 是两份额度，不共享。** 三重证据：

1. **项目文档**：README 首屏「**双额度通道**: 每个账户同时拥有 Playground 与 Build 应用代理**两份独立额度**，`UPSTREAM_CHANNELS` 可单独或同时启用；一个通道触发限额后，同一账户由另一个通道继续」；`docs/build.md:3`「AI Studio 的 Build 应用经官网宿主页调用 MakerSuite 代理 RPC 访问 Gemini API，**这部分调用使用与 Playground 分开计算的额度**……同一账户同时拥有两份额度」；`docs/protocol.md:957` 同义。
2. **运行态实现**：冷却按通道分键 —— Build 撞限额写 `build:<模型>`，Playground 写 `<模型>`（`docs/waa.md:656`）。共享的话不需要分键。
3. **社区实测容量不同**（[issue #58](https://github.com/Mag1cFall/AIStudio2API/issues/58)，2026-10）：同一模型 `gemini-3.1-flash-lite-image`，**Playground 约 50 张/天，Build 只有 15-16 张/天**（Pro 与 Free 在 Build 上同容量）。同 issue 里作者原话：「build 一共就只有这些池子，Antigravity 还有生图额度」。

**会被误当成"共享"的三样东西**（确实共享，但不是额度）：账户身份与 Cookie、**WAA VM**（`docs/build.md:77`「由账户的同一个 WAA Worker 生成，与 Playground 共用 VM」）、请求头集合（`docs/build.md:73`「与 Playground 文本请求的 GenerateContent 相同」）。另有一种独立情形：在 Build 里**填自己的付费 API key** 时，宿主页携带该 key 直连 Gemini API，走 **API key/project 的配额**（`docs/build.md:7`），跟网页额度体系无关。

**官方口径的边界**（`ai.google.dev/gemini-api/docs/rate-limits`，经 r.jina.ai 读到原文）：官方只文档化 **Gemini API** 的限额 —— 「Rate limits are applied **per project, not per API key**」、RPM/TPM/RPD 三维度、**RPD 在太平洋时间午夜重置**（= 北京时间 15:00 夏令时 / 16:00 冬令时）。这与社区观测对得上：issue #45 报「分钟级 TPM 被误判为全天限额，要第二天 15 点才解封」—— 15 点正是太平洋午夜。**网页端 Playground/Build 的额度关系，官方没有公开表格**，只能登录后到 `aistudio.google.com/rate-limit` 自查。

- 账户权益分 **Free / Pro / Ultra / Plus**；只有 Ultra 开放的模型同样列在目录里，但**调用时 403 并写明所需订阅**（`docs/protocol.md` 模型节）。Build 的 Nano Banana 2.1 疑似要 Ultra（issue #52），但作者自己也没确认是「限 Ultra」还是「全局池子限额」，原话：「我只成功了一次，所以仍然不确定」。
- 模型目录是**运行时从上游实时拉的**，随 AI Studio 更新变化 —— 不要写死模型名，用 `MODEL_MAPPING` 做别名。

### 4.4 模型名现状（Mag1cFall 抓的当日目录，节选）

`gemini-3.8-flash` 系列、`gemini-3.1-pro-preview`、`gemini-3.1-flash-lite`、`gemini-3-flash-preview`、`gemma-4-31b-it` / `gemma-4-26b-a4b-it`、Nano Banana 2（`gemini-3.1-flash-image`）、Veo 3.1、Lyria 3、`gemini-omni-flash-preview`、`antigravity-preview-05-2026`。

---

## 5. 边界：这些**不是** Playground 反代

| 赛道 | 代表项目 | 与 Playground 反代的区别 |
|---|---|---|
| **Gemini 网页版**（gemini.google.com） | HanaokaYuzu/Gemini-API（3600★）、zexadev/gemini-web2api-go（614★）、ntthanh2603/gemini-web-to-api（588★） | 身份是 `__Secure-1PSID` + `SNlM0e`，端点是 `batchexecute`；额度池、封号面、模型集全都不同 |
| **Build App 反代** | iBUHub/AIStudioToAPI（1780★，**CC BY-NC 4.0 = 禁商用**）、cliouo/aistudio-build-proxy-all（278★）、hkfires/AIStudioBuildWS（61★） | 走 Build 应用代理 RPC，额度与 Playground 分开 |
| **API key 池** | 各 one-api / new-api 渠道（聚星已有） | 官方 key，额度最小、风控最轻 |

**如果"playground 反代"你指的是 Gemini 网页版那一支，说一声，那份谱系我另开一份。**

---

## 6. 接聚星的判断

**首选：Mag1cFall/AIStudio2API（`WAA_BACKEND=go`）+ saubaka 的双渠道入口语义。** 理由：

1. **唯一的活跃 Go 实现**，今天还在推代码；纯 Go 后端不拖浏览器进程，2 核 1.6 G 的机器能常驻（把 `WARM_WORKER_LIMIT`/`MAX_ACTIVE_WORKERS` 调到 2/3）。
2. **MIT**，可商用、可改、可闭源分发 —— 对需要商用的场景是硬门槛。
3. 它把 **Playground 与 Build 两个额度池**同时接进同一个进程，聚星侧只需要在 new-api 里加一个 OpenAI 兼容渠道（`/v1`），就能拿到"一条渠道、两个池子"的供给；想要**渠道级计费隔离**，照 saubaka 的 `/playground/v1`、`/build/v1` 双入口改一下即可。
4. 文档自带 126 KB 协议规范 + 62 KB WAA 实现 —— 自研对账时不用猜。

**次选：chrysoljq/aistudio-api**（如果只想跑 Python、只要 Playground 一条通道、且不在意 2 个月没更新）。它的源码结构是这批里最好读的，`account_rotator` 的 `least_rl` 策略（按被限流次数最少选号）比纯 round-robin 更贴合"号池轮换"。

**不建议**：CJackHwang（AGPLv3 + Ⅰ 代 UI 自动化 + 停更 6 个月）作为生产主力；它的价值是文档和社区经验。

**要提前解决的三个坑**（都会有代价，先认领再动手）：

| 坑 | 事实 | 影响 |
|---|---|---|
| 登录后**续签绑 Windows** | DBSC 续签材料由 Windows Chrome 导入，Linux/Docker 中失效需回 Windows 重导 | 服务器上跑 = 需要一台 Windows 常驻机做续签源，或接受定期重登 |
| **代理出口** | 账户可绑 per-account 代理，且指纹与出口一起固定 | 账号与出口 IP 必须长期稳定；换 IP 频繁＝风控直接命中 |
| **封号面未知** | 我没有跑过任何一条，也没有这批项目的封号率实测数据 | 只投产号，不投产"主力号"；先把 Playground 当**冗余通道**而不是替换 gcli |

---

## 7. 未验证清单（别当结论用）

- 这四个项目**本机一个都没跑过**（本机无 Docker，也没登录 Google 账号）—— 部署步骤、端口、端点全部来自 README/源码，未经运行验证。
- 星数/最后推送是 **2026-10-16 的 GitHub API 快照**；GitHub 未认证 API 在抓取中触发过限流，部分 repo 详情（forks/issue 数）没取到。
- **封号率、单账号 Playground 日额度、限流触发阈值**：上游决定，且随时变。任何"每天多少请求"的说法都要自己压测。
- chrysoljq 的 `MITMProxy`（本地 7861）在它的实际请求链里承担什么角色，源码里没有调用点说明 —— 我读了 `proxy.py` 全文，它更像"调试/抓包通道"而非生产链路（业务链是 `gateway/session.py` 53 KB + `wire_codec.py`）。这条判读**未验证**。

---

## 附：本地留存

```

├── Playground反代调研.md          （本报告）
├── raw\                           11 份原始文档（各家 README + Mag1cFall 的 protocol.md 178KB / waa.md 62KB / build.md 30KB / development.md 36KB / reverse-engineering.md 28KB）
├── raw\issues\                    3 份 GitHub issue 快照（#58 额度讨论 / #52 Build 生图额度 / #45 限流误判）
└── src\aistudio-api-master\       chrysoljq/aistudio-api 全量源码（90 文件 / 3.8 MB）
```
