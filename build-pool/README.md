# Build 多账号顺延：四条路线与取舍

问题：某商业站 那套（以及任何照抄它协议的自建网关）做不到「一个账号用完顺到下一个」，因为站端拿不到额度。
本文给出能做到的四条路线、各自代价、以及已经跑通的调度器实现。

## 核心判断：先纠正一个目标

「用完一个再用下一个」不是最优策略，正确目标是 **优先消耗最早重置的那份额度**。

额度是按窗口重置的（5h / 7d 双窗口，前面实测 `retrieveUserQuotaSummary` 就返回 `bucketId + window + remainingFraction + minResetDays`）。
如果 A 号 3h 后重置、B 号 24h 后重置，先打 A 是因为 A 那份不用就过期；等 A 真的耗尽再换，A 剩下的 20% 就白扔了。

实测输出（`python scheduler.py`）：

```
请求序列（先耗尽最早重置的额度；第 2 次打 429）
  #1 -> E2 acct-b*** rem=1.00 200      ← E2 3h 后重置，先打
  #2 -> E2 acct-b*** rem=0.98 429      ← 打爆，进冷却
  #3 -> E1 acct-a*** rem=0.08 200      ← 顺延，余量少的号接着用
  #4 -> E1 acct-a*** rem=0.06 200
  #5 -> E3 acct-c*** rem=1.00 200      ← E1 跌破 5% 阈值进入 draining
  #6 -> E3 acct-c*** rem=0.98 200
调度顺序: ['E2','E2','E1','E1','E3','E3']
```

「顺延」在这套实现里是状态机的自然结果，不是特判。

## 路线对比

| 路线 | 改什么 | 能真正做到「顺延」吗 | 代价 | 风险 |
|---|---|---|---|---|
| **0. 多 profile 手动** | 不改任何代码 | ❌ 只能并发，不能顺延 | 每号一个浏览器 profile | 人肉维护登录态，掉线即断 |
| **1. 站端失败驱动调度** | 只改服务端（`scheduler.py` 可直接用） | ⚠️ 事后顺延：429 之前那一次会打给用户 | 小，纯服务端 | 每个号的第一次 429 都要用户吃 |
| **2. executor 上报额度** | 服务端 + executor 脚本 | ✅ 事前顺延，接近零失败 | 中，要给每个 executor 加额度探针 | 依赖页面能否读到额度（待验证） |
| **3. 服务端浏览器农场** | Playwright/Camoufox + 账号托管 | ✅ 完全可控 | 大，每号一个浏览器上下文 | 指纹、登录态、风控、内存 |

推荐 **2 为主 + 1 兜底**：路线 1 已经零成本可用，路线 2 只补一个数据源，不需要重写架构。路线 3 只在要完全无人值守时才值得。

## 路线 1：失败驱动（已实现）

`scheduler.py` 是一个完整的状态机，无第三方依赖：

- 状态：`ready / draining / cooling / dead`
- 每 executor 并发 1（Playground 的 WAA proof 绑输入框必须串行；Build 同样按 1 处理最稳）
- `report_quota(eid, family, remaining, reset_at)` —— 额度快照入口，有快照就能事前判断，没快照就退化为 `release()` 里的粗略递减
- `release(eid, model, ok, status)` —— 429 冷却到 `reset_at`，401/403 直接 `dead`，5xx 退避 `30s × 失败次数`（上限 4 倍）
- `pick(model)` —— 打分键 `(reset_at, remaining, inflight, served)`，最小者优先

接进现有 WS 服务只要三处：

```python
ex = pool.acquire(model)              # 收到 /v1/chat/completions 时
ws.send({"type":"request", "requestId":rid, "model":model, "stream":stream, "requestBody":body})
...
pool.release(ex.eid, model, ok=(status==200), status=status)   # 收到响应时
```

自检已通过（`assert` 全绿，exit=0），包含 429 顺延、draining 阈值、重置后复活三组断言。

## 路线 2：executor 上报额度（关键一步）

要在 429 之前就换号，必须有额度快照。协议只需加一种消息，不改请求路径：

```js
// executor 侧，每 60s 或每 N 次请求上报一次
wsSend({
  type: "quota",
  snapshots: [{
    family: "gemini-3.7-flash",     // 与 build-*/pg-* 前缀解耦的家族键
    remainingFraction: 0.62,
    resetAt: 1760000000,            // unix 秒
    bucketId: "5h",                 // 5h / weekly
    tier: "ultra"                   // free / pro / ultra
  }]
});
```

服务端 `report_quota()` 直接吃这个结构。

**成败点：页面能不能读到额度。** 这一点本项目尚未验证 —— 某商业站 的 Build/Playground executor 都不上报额度，`/api/build/status` 也没有 per-connection 额度字段，所以没有任何现成证据说明 Build App 页面暴露了额度接口。

`probe_quota.js` 就是拿来验这一步的：粘进 Build App 的 console，然后去页面里发一条消息或刷新额度面板，看有没有 `remainingFraction / resetTime / bucketId` 的响应。

- 有 → 路线 2 成立，前端加 20 行上报即可。
- 没有 → 只能用路线 1（事后顺延），或者走路线 3 用 Playground 页面的 `retrieveUserQuotaSummary`（这条我们已经在 gcli2api 上游确认存在）。

## 路线 3：服务端浏览器农场

`build-server-znpos901` 那条路的完整版：每号一个 Playwright/Camoufox context，服务端自己把脚本粘进 Monaco、点 Preview、抓响应。

- 顺延 = 切 context，额度可读（自己控制页面，能直接调页面内的 quota API）
- 代价：每号 300–800MB 内存、启动 5–15s、指纹要一致、登录态要持久化（storageState）
- 只在「不想让用户开着标签页」时值得：比如给下游客户开放 API、自己当上游

## 与 某商业站 现状的差距

| 能力 | 某商业站 现在 | 路线 1 | 路线 2 |
|---|---|---|---|
| 多账号并发 | ✅ 多连接（`max_connections:10`） | ✅ | ✅ |
| 看见每个号用了多少 | ❌ | 部分（内部计数） | ✅ |
| 429 后不再打同一号 | ❓ 未知（引擎侧） | ✅ | ✅ |
| 429 之前就换号 | ❌ | ❌ | ✅ |
| 对客户端透明 | ✅ | ✅ | ✅ |

结论：**某商业站 停在第一行**。它的 `connections` 只是连接计数，没有 per-connection 的额度或成败账本，所以最多做到「失败后不再用这个连接」，做不到「提前顺延」。

## 落地顺序

1. `python scheduler.py` 跑通自检（已完成，exit=0）
2. 把 `Pool` 挂到自己的 WS 服务上，先只做路线 1 —— 验证多连接确实在分流
3. 用 `probe_quota.js` 确认额度数据源
4. 有数据源就加 `quota` 消息走路线 2；没有就评估路线 3
5. 下游用 `/v1/models` 只暴露聚合后的模型名，轮换对客户端不可见
