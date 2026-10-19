# Build / Playground 双通道反代

AI Studio 有两条独立的额度通道，同一个 Google 账号在两边的配额**互不相干**：

| 通道 | 入口 | 调用方式 | 额度特征 |
|---|---|---|---|
| **Build** | `aistudio.google.com` 的 Build App 代理 | `ProxyUnaryCall` / `ProxyStreamedCall`（gRPC-Web） | 每个 flash 模型约 100 次/账号/天，pro 约 10 次 |
| **Playground** | 官网 Playground | `GenerateContent`（REST/gRPC） | argon 类模型约 50 次/账号/天 |

一个通道打满时，**同一个账号**可以立刻切换到另一个通道继续服务——这是双通道反代的核心价值。

## 快速开始

**服务器一键安装**（Linux，systemd）：

```bash
# 先下载二进制放到 deploy/ 目录
#   https://github.com/Mag1cFall/AIStudio2API/releases
scp -r deploy/ aistudio2api-linux-amd64 root@server:/root/relay/
ssh root@server 'cd /root/relay/deploy && cp ../aistudio2api-linux-amd64 aistudio2api && sudo bash install.sh'
```

装完 `ready=0` 属正常——还没有账号。加账号（在有图形界面的机器上）：

```bash
ssh -N -L 2048:127.0.0.1:2048 root@server     # 终端 1，保持
bash deploy/login-account.sh --server http://127.0.0.1:2048   # 终端 2
```

完整部署文档：[`docs/deployment.md`](docs/deployment.md)（三种形态、Nginx 配置、排障）

## 这里有什么

```
deploy/
  install.sh            服务器一键安装（建用户/装二进制/写 .env/systemd/自检）
  login-account.sh      本机隔离登录并推送到服务器
  check_account.py      诊断账号能不能用、能不能续签
  .env.example          配置模板（每个变量的取值理由都写在注释里）
watchdog/
  watchdog.py           健康检查（服务/账号/推理/续签能力/死代理）
  install-watchdog.sh   装成 systemd timer
docs/
  deployment.md         部署与排障
  build-playground-relay.md     双通道反代综述
  playground-protocol.md        Playground GenerateContent 协议分析
build-pool/
  README.md             多账号顺延的四种路线与取舍
  scheduler.py          调度器实现（python scheduler.py 可直接跑）
  api_contract.json     接口契约快照
  _contract_probe.py    契约探测脚本
  probe_quota.js        额度探测
```

## 核心结论（先说答案）

**1. 双通道不是"负载均衡"，是"额度拼接"。**

`gemini-4-argon` 在 Build 通道上，Google 给的是 `limit: 0` —— 不是"用完了"，是**这个模型在任何账号的 Build 通道都没有配额**：

```
Quota exceeded for metric: generate_requests_per_model_per_user,
  limit: 0, model: gemini-4-argon
```

而它在 Playground 通道正常可用。反过来 `gemini-3.8-flash` 在 Build 通道有 100 次/天，Playground 却慢得多。

**所以通道选择必须按模型决定，不能全局开关。** 全局把 `playground` 排到前面会让 argon 从 3s 变成 22–56s（Playground 的 `GenerateContent` 是流式收集后转非流式，比 Build 的原生非流式慢一个数量级）。

**2. "用完一个账号顺延到下一个"不是最优策略。**

正确目标是**优先消耗最早重置的那份额度**。如果 A 号 3 小时后重置、B 号 24 小时后重置，应该先打 A —— 因为 A 那份不用就过期。等 A 真耗尽再换，A 剩下的 20% 就白扔了。

`scheduler.py` 就是这个状态机的参考实现：

```
请求序列（先耗尽最早重置的额度；第 2 次打 429）
  #1 -> E2 acct-b*** rem=1.00 200      ← E2 3h 后重置，先打
  #2 -> E2 acct-b*** rem=0.98 429      ← 打爆，进冷却
  #3 -> E1 acct-a*** rem=0.08 200      ← 顺延，余量少的号接着用
  #5 -> E3 acct-c*** rem=1.00 200      ← E1 跌破 5% 阈值进入 draining
调度顺序: ['E2','E2','E1','E1','E3','E3']
```

「顺延」在这里是状态机的自然结果，不是特判。

**3. 账号能否续签，看 `origins` 而不是看它现在能不能用。**

从 Chrome 导入的账号，`storage-state.json` 里 `origins: []` 是空的 —— 没有 `aistudio.google.com` 的 localStorage 就拿不到 `AuthExtension`，续签代码根本启动不了。

现象是：账号现在是 `ready`，cookie 还没过期，看起来一切正常；但每次续签都在后台失败，日志只有一句误导性的「Chrome OAuth 导入仅支持 Windows」。等 cookie 真过期（可能 300 多天），它才掉成 `auth_required`。

**Chrome 导入只给 cookie，从来不给 origins。** 所以从 Chrome 导的账号在 Linux 上必然全废，把本机文件推上去也救不回来（本机那份同样是残缺的）。唯一出路是 **Camoufox 隔离登录**（`setup --login --remote`）——它跑的是真浏览器，会把 localStorage 一起抓下来。

## 部署形态

```
本机（Windows/macOS，有图形界面）
  └─ setup --login --remote  →  Camoufox 弹出可见窗口，人工登录
       └─ 一次性生成 storage-state（含 origins）+ 指纹
            └─ 通过 pairing 接口推到服务器

服务器（Linux，无图形界面）
  └─ 常驻服务，读 auth/ 目录里的凭据
       └─ 对上游：Build 通道 / Playground 通道 按模型调度
       └─ 对下游：暴露 OpenAI / Gemini / Anthropic 三种兼容接口
```

**服务器端不需要 Camoufox**（`WAA_BACKEND=go` 是纯 Go 实现，不起浏览器）。这让内存占用从 700MB/账号降到 350MB/全部账号。

## 踩过的坑

**pairing 接口的字段名是 `label`**，不是 `email`/`account`。传错不报字段错，而是报「账户必须填写 Google 邮箱」——因为它在 `accountEmailID()` 里拿不到候选。

**pairing 只用于新增账号**。已存在的 label 一律 409 `account_exists`，而 `importIsolatedLogin` 的实现是：

```go
defer os.RemoveAll(loginDirectory)   // ← 抓到的 storage-state 在这里
saved, err := target.Save(...)       // ← 409 在这里失败，数据随 defer 一起没了
```

**登录抓到的完整状态被直接丢弃**，必须先在服务器删掉同名旧记录再重登。

**`BUILD_NATIVE_NONSTREAM=true` 会让非流式请求无条件优先 Build 通道**，绕过 `UPSTREAM_CHANNELS` 的顺序。想按通道顺序调度必须同时把它设成 `false` —— 但那会让所有非流式请求都走慢的 Playground，得不偿失。

**`-build` 后缀的模型（`gemini-4-argon-build`）走 assistant 通道**，单次 17–65 秒，超过网关 60 秒首字节超时就直接 502。这类模型必须从可用列表里排除。

## 额度实测

| 通道 | 模型 | 实测上限 | 依据 |
|---|---|---|---|
| Build | `gemini-3.8-flash` | **100 次/账号/天** | 错误原文 `limit: 100` |
| Build | `gemini-4-argon` | **0（结构性无配额）** | 错误原文 `limit: 0` |
| Playground | `gemini-4-argon` | ≥40 次未触发 | 单日最高 40 次，无 429 |
| Build | `gemini-3.1-pro-preview` | 未验证 | 样本仅 3 条 |

注意：**监控探针会作弊**。如果探针每 5 分钟打一次同一个模型，一天 288 次，会把 100 次的额度吃掉一半以上。实测某天 build flash 成功 141 次里有 76 次是探针，直接导致「才 49 次就撞限额」。探针要选业务不用的模型。

## 协议要点

Build 通道和 Playground 通道的协议完全不同，但对外都可以包装成 OpenAI 兼容格式。细节见：

- [`docs/playground-protocol.md`](docs/playground-protocol.md) — `GenerateContent` 的请求/响应结构、流式帧格式、额度字段
- [`build-pool/README.md`](build-pool/README.md) — 多账号调度的四种路线对比与实现

## License

文档与自研代码（`build-pool/scheduler.py`、`_contract_probe.py`、`probe_quota.js`）采用 MIT。

协议分析部分是对公开 Web 接口的黑盒观察，用于互操作性研究。
