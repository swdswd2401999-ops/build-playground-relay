# Build / Playground 双通道反代综述

## 一、两条通道是什么

AI Studio 给每个 Google 账号提供两套独立的调用入口，配额互不相干：

```
                    ┌─────────────────────────────┐
                    │   Google AI Studio 账号      │
                    └──────────┬──────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            │                                     │
    ┌───────▼────────┐                  ┌─────────▼────────┐
    │  Build 通道     │                  │  Playground 通道  │
    │  Build App 代理 │                  │  官网 Playground  │
    ├────────────────┤                  ├──────────────────┤
    │ ProxyUnaryCall │                  │ GenerateContent  │
    │ ProxyStreamed  │                  │ (REST / gRPC)    │
    ├────────────────┤                  ├──────────────────┤
    │ flash ~100/天   │                  │ argon ~50/天      │
    │ argon  0        │                  │ 其他模型视账号     │
    │ pro    ~10      │                  │                  │
    └────────────────┘                  └──────────────────┘
```

**关键：这两个额度不能互相挪用，但同一个账号可以在两边都有额度。**

所以「双通道反代」的意义不是负载均衡，而是：

> 某个模型在 A 通道没额度时，同一个账号从 B 通道继续服务，不换账号、不降级。

## 二、为什么必须按模型选通道

### 反例：全局把 Playground 排前面

看起来很合理（Playground 是"官方入口"），实测结果是灾难：

```
argon → playground | 模式=stream | 回退流式 | RPC=GenerateContent   → 22s / 56s
argon → build      | 模式=native | RPC=ProxyUnaryCall               → 2.8s / 4.5s
```

**差一个数量级。** 原因是 Playground 的 `GenerateContent` 返回的是流式帧，要完整收集后转成非流式响应；而 Build 的 `ProxyUnaryCall` 本身就是一次非流式调用。

### 正解：通道选择由「模型在哪个通道有配额」决定

```
gemini-4-argon        → Build 通道 limit:0（结构性无配额）→ 必须走 Playground
gemini-3.8-flash      → Build 通道有 100 次/天，且 Build 更快 → 优先 Build
gemini-3.1-pro-preview → 两个通道都有，按剩余额度选
```

`limit: 0` 和「今天用完了」是两件事：

| | 错误原文 | 能恢复吗 |
|---|---|---|
| 日限额用完 | `limit: 100` + `Please retry in 19h` | ✅ 到下一个额度日 |
| **结构性无配额** | **`limit: 0`** | ❌ **永远不恢复** |

`limit: 0` 的 `retry in 19h` 是 Google 给的固定退避时间，到点重试仍然是 `limit: 0`。识别方法就是看错误体里那个数字。

## 三、账号生命周期：真正的死线是 localStorage

### 三种 "storage-state" 的差别

| 来源 | cookies | `origins` | 能续签吗 |
|---|---|---|---|
| Chrome 导入 | 30–40 个 | **空** | ❌ 永远不能 |
| Camoufox 隔离登录 | 44–53 个 | 2 个（含 aistudio） | ✅ 能 |
| 手工拼装 | 视情况 | 通常空 | ❌ 大概率不能 |

`origins` 里必须有 `https://aistudio.google.com` 的 localStorage，否则拿不到 `AuthExtension`（OAuth 扩展），续签代码启动的第一步就失败。

### 误导性的错误信息

```
账户认证续签失败 | 耗时=1ms | 错误=Chrome OAuth 导入仅支持 Windows
```

`耗时=1ms` 说明它根本没走网络——在本地分支就返回了。这句错误让人以为是「平台限制，换个平台就行」，实际是**这个账号的凭据文件缺少续签所需的材料**。

### 症状的滞后性

失效账号的 cookie 可能还有 **364 天**才过期。所以在 `origins` 为空之后的三百多天里：

- `/api/accounts` 显示 `ready`
- 请求正常工作
- 续签每 5 分钟失败一次，只写进日志

直到 cookie 真过期，才集体掉成 `auth_required`。

**判据不是"能不能用"，是"能不能续签"。**

```
origins 含 aistudio.google.com  →  能续签
origins 为空                    →  会在 cookie 到期那天暴毙，与当前可用性无关
```

## 四、Camoufox 隔离登录

服务器上跑不了（没有 X11/Xvfb），必须在有图形界面的机器上做：

```
本机
  1. aistudio2api setup --login --remote <服务器地址>
     → 弹出可见浏览器窗口，人工登录 Google
     → 抓取完整 storage-state（含 origins）+ 浏览器指纹
  2. 通过 pairing 接口上传到服务器

服务器
  3. 收到后写入 auth/<email>/storage-state.json
```

### 三个必须知道的细节

**1. pairing 接口的字段名是 `label`**

```json
{"label": "user@gmail.com", "storage_state": {...}}
```

传 `email` / `account` / `mail` 都会被忽略，然后报「账户必须填写 Google 邮箱」——因为它从 `label` 里取候选。

**2. pairing 只用于新增**

已存在的 label 返回 409 `account_exists`。而上传函数的实现是：

```go
defer os.RemoveAll(loginDirectory)   // 临时目录，里面是刚抓到的 storage-state
saved, err := target.Save(...)       // 409 在这里失败
```

登录成功抓到的数据**随 defer 一起被删掉**。所以必须先在服务器删掉同名旧记录（`DELETE /api/accounts/{id}`），再重新登录。

**3. 一次只能登一个邮箱**

`--login` 不支持批量。每个账号要单独走一遍，含 Google 二次验证约 5–10 分钟。

## 五、部署配置矩阵

| 配置项 | 推荐值 | 为什么 |
|---|---|---|
| `WAA_BACKEND` | `go` | 纯 Go 后端不起浏览器，4 账号 350MB（Camoufox 方案 700MB/账号） |
| `UPSTREAM_CHANNELS` | `build,playground` | 双通道都开，具体走哪个由模型决定 |
| `BUILD_NATIVE_NONSTREAM` | `true` | 关掉会让所有非流式都走慢的 Playground |
| `PER_ACCOUNT_CONCURRENCY` | `2` | 单账号并发越高越容易撞日限额（429 按账号+模型计） |
| `WARM_WORKER_LIMIT` | = 可用账号数 | 热池目标是常驻预热，多配无意义 |
| `MAX_ACTIVE_WORKERS` | 2 × 可用账号 | 按需扩容上限 |
| `REQUEST_TIMEOUT` | 建议显式设 | 默认 30 分钟，与 nginx 的 900s 不一致会造成"请求占着并发槽到超时" |

### 内存账

```
纯 Go 后端 4 账号          ~350MB
纯 Go 后端峰值             446MB
MemoryMax（建议）          900MB
MemoryHigh（建议）         1.2GB
```

⚠️ **Go runtime 不认 cgroup 的 `MemoryHigh`。** 达到阈值时 cgroup 会 throttle 分配（进程变慢），但 Go 的 GC 只看 `GOGC=100`（堆翻倍才回收）。要让它主动工作在限制以下，必须加：

```
Environment=GOMEMLIMIT=900MiB
```

否则表现是「明明没到 MemoryMax，却突然卡住」。

## 六、监控的坑

### 探针会吃掉业务配额

每 5 分钟一次的存活探针 = 288 次/天。如果它打的是 `gemini-3.8-flash`（100 次/账号/天），会占掉一半以上。

实测某天：

```
当日 build flash 成功 141 次
其中探针 76 次 = 54%
某账号 49 次里 30 次是探针  →  「才 49 次就撞 100 限额」
```

**探针必须选业务不用的模型**，或者改用不消耗生成配额的健康检查。

### 账号状态不等于续签健康

`/api/status` 只报 `ready: N`，不反映续签是否在工作。需要单独扫 `origins`：

```python
for each account:
    ss = json.load(storage_state.json)
    renewable = any('aistudio.google.com' in o['origin'] for o in ss['origins'])
```

### 续签是否活跃：看 mtime，不看 cookie 剩余天数

可续签的账号，续签时会把 `storage-state.json` 的 mtime 推新。

```
✅ 健康   mtime 0.0h 前   cookies 53   剩余 30 天
❌ 冻结   mtime 3.9h 前   cookies 40   剩余 -0.2 天
```

**不要用「所有 cookie 里最早的 expires」判断寿命** —— 那些 `__Secure-*PSIDRTS` 之类的 cookie 早就死了，会把「剩余天数」永远算成负数。只取会话型 cookie（`SID` / `SAPISID` / `__Secure-*PSID`）。

## 七、快速自检清单

```
□ 账号 origins 非空？          grep -c aistudio.google.com auth/*/storage-state.json
□ 续签 mtime 新鲜？            find auth -name storage-state.json -mmin -60
□ 探针模型不是业务主力？        查 probe 请求落在哪个 model
□ limit:0 的模型是否已排除？    看冷却 reason 里有没有 "limit: 0"
□ 通道顺序是否被 BUILD_NATIVE 覆盖？  env | grep BUILD_NATIVE
□ GOMEMLIMIT 设了吗？          env | grep GOMEMLIMIT
□ 并发上限 = 账号数 × PER_ACCOUNT_CONCURRENCY？
□ 冷却到点会自动恢复（不需重启），但 runtime-state 里的死条目会跨重启留存
```
