# 部署

三种形态，按你的场景选：

| 场景 | 走哪条 | 需要什么 |
|---|---|---|
| 就想跑起来试试 | [A. 本机单机](#a-本机单机docker-可选) | Windows/macOS，8G 内存 |
| 要给别人用 / 长期跑 | [B. Linux 服务器](#b-linux-服务器推荐) | VPS，2G 内存起 |
| 有一堆账号要轮换 | [C. 多账号池](#c-多账号池) | 多个 Google 账号 |

所有形态的共同前提：**账号必须用 Camoufox 隔离登录，不能 Chrome 导入**。原因见文末。

---

## A. 本机单机（Docker 可选）

只有你一个人用、或者先验证能不能跑通。

```bash
# 1. 拿二进制（Linux 是同一套参数）
#    https://github.com/Mag1cFall/AIStudio2API/releases
#    windows-amd64 / darwin-arm64 都可以

# 2. 首次配置 + 登录（会弹浏览器）
./aistudio2api.exe setup --login

# 3. 启动
./aistudio2api.exe
```

默认监听 `127.0.0.1:2048`，管理界面 `http://127.0.0.1:2048/admin`。

验证：

```bash
curl -s http://127.0.0.1:2048/api/status
# {"state":"RUNNING","running":true,"ready":true,...}

curl -s http://127.0.0.1:2048/v1/models \
  -H "Authorization: Bearer $(grep ^PROXY_API_KEY .env | cut -d= -f2-)"
```

**注意**：本机跑的时候不要同时开代理软件的全局模式。`WAA_BACKEND=go` 走直连，系统代理会把它带去错误出口，表现为无响应的超时而不是明确报错。

---

## B. Linux 服务器（推荐）

```bash
# 1. 传二进制和脚本上去
scp -r deploy/ user@server:/root/relay/
scp aistudio2api-linux-amd64 user@server:/root/relay/deploy/aistudio2api

# 2. 装
ssh user@server
cd /root/relay/deploy
sudo bash install.sh
```

脚本会做 6 件事：

```
建系统用户 aistudio（含 ~/.cache）
装二进制到 /opt/aistudio2api
生成 .env（PROXY_API_KEY 随机 47 位）
装 systemd unit（含 StartLimit 熔断 + GOMEMLIMIT）
启动并自检
打印后续步骤
```

装完 `ready=0` 是正常的 —— 还没有账号。

### 加账号

服务器只监听 `127.0.0.1`，所以要在**有图形界面的机器**上登录再推过去。

```bash
# 终端 1：建隧道（保持不关）
ssh -N -L 2048:127.0.0.1:2048 root@<server>

# 终端 2：登录并推送
bash deploy/login-account.sh --server http://127.0.0.1:2048
```

会弹出 Camoufox 窗口（Firefox 内核），手动完成 Google 登录。成功后状态自动上传，窗口关闭。

验证：

```bash
ssh root@<server>
python3 /opt/aistudio2api/check_account.py --all /opt/aistudio2api/auth
curl -s http://127.0.0.1:2048/api/status
```

### 装看门狗

```bash
sudo bash watchdog/install-watchdog.sh
```

每 5 分钟检查：服务活着、有 ready 账号、真实推理能通、每个账号**能不能续签**、有没有绑死代理。只在状态变化时写日志，不会刷屏。

```bash
journalctl -u aistudio-watchdog -n 20
cat /var/log/aistudio-watchdog.log
```

### 对外暴露

默认只在回环。要让外部用，二选一：

**Nginx 反代**（推荐，能被 CDN 挡在前面）：

```nginx
location /v1/ {
    proxy_pass http://127.0.0.1:2048;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;

    # 流式必须关缓冲，否则客户端要等到全部生成完才收到第一个字
    proxy_buffering off;
    proxy_cache off;

    # 非流式长请求最长可达 10 分钟（见 .env 的 REQUEST_TIMEOUT）
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;
}

# 管理端不建议暴露到公网
location /admin { deny all; }
location /api/internal { deny all; }
```

**直连**（不推荐）：

```bash
# .env 改三处，否则等于把管理接口挂公网
LISTEN_ADDR=0.0.0.0:2048
ADMIN_AUTH_ENABLED=true
# 再加防火墙只放行来源 IP
```

---

## C. 多账号池

多个账号时，关键不是"加更多账号"，而是**调度策略**。

### 前提：每个账号都要能续签

```bash
python3 deploy/check_account.py --all /opt/aistudio2api/auth
```

输出里 `origins` 那一行是命门：

```
✅ a01077522795@gmail.com
   · origins          2 个
   · 最后刷新         0.0h 前

❌ rondaelmasv45@gmail.com
   · cookies          40 个
   ✗ origins          0 个 ← 没有 aistudio.google.com，续签代码永远启动不了
```

`origins=0` 的账号**现在能用，但 cookie 到期那天直接掉线**，中间没有任何征兆。这种账号唯一的修法是重新隔离登录。

### 分配并发

```
单账号并发  PER_ACCOUNT_CONCURRENCY=2
常驻预热    WARM_WORKER_LIMIT=<可用账号数>
扩容上限    MAX_ACTIVE_WORKERS=2 × <可用账号数>
```

实测：单账号并发从 2 提到 4，一天内 429 次数翻倍。因为配额按「账号 + 模型」计，并发高只是更快撞墙。

### 额度分配

| 通道 | 模型 | 上限 | 说明 |
|---|---|---|---|
| Build | `gemini-3.8-flash` | 100 次/账号/天 | 错误原文 `limit: 100` |
| Build | `gemini-4-argon` | **0** | 结构性无配额，换多少次都是 0 |
| Playground | `gemini-4-argon` | ≥40 | 单独额度池 |

`limit: 0` 和"用完了"要分清：

```
limit: 100 + retry in 19h   → 明天的额度日恢复
limit: 0   + retry in 19h   → 永远不恢复，是权限问题
```

### 多账号顺延

`scheduler.py` 是实现参考。核心结论：**不是"用完一个换下一个"，而是优先消耗最早重置的那份**。

```bash
python scheduler.py
```

```
请求序列（先耗尽最早重置的额度）
  #1 -> E2 acct-b*** rem=1.00 200      ← 3h 后重置，先打
  #2 -> E2 acct-b*** rem=0.98 429      ← 打爆，进冷却
  #3 -> E1 acct-a*** rem=0.08 200      ← 顺延
```

---

## 为什么不能用 Chrome 导入

这是整套东西最容易踩的坑，也是最贵的。

| | Chrome 导入 | Camoufox 隔离登录 |
|---|---|---|
| cookies | 30–40 个 | 44–53 个 |
| `origins` | **0（空）** | **2（含 aistudio）** |
| 续签 | ❌ 永远失败 | ✅ 正常 |
| 当前可用 | ✅ | ✅ |

`origins` 为空就拿不到 `AuthExtension`（OAuth 扩展），续签代码启动第一步就返回：

```
账户认证续签失败 | 耗时=1ms | 错误=Chrome OAuth 导入仅支持 Windows
```

`耗时=1ms` 说明它根本没走网络 —— 这是本地分支，与平台无关。看起来像"换个系统就行"，实际是**这份凭据缺材料**。

后果是延迟 300 多天爆发：cookie 的 `expires` 还有 300+ 天，这期间账号一直是 `ready`，请求正常，只有日志里每 5 分钟失败一次。等 cookie 真过期，一批账号同时下线。

**判断标准不是"现在能不能用"，是"能不能续签"。**

---

## 排障

### 服务起不来：`status=226/NAMESPACE`

```
Failed to set up mount namespacing: /opt/aistudio2api/auth: No such file or directory
```

不是权限问题，是 `ReadWritePaths=` 指向的目录不存在，mount namespace 建不起来。两种触发：

1. 装到 `/tmp` 下 —— unit 里 `PrivateTmp=yes` 会把 `/tmp` 换成私有 tmpfs，原路径消失
2. `auth/` 或 `runtime/` 没建 —— 脚本会建，手工装的话要自己 `mkdir`

### 服务起不来：`bind: address already in use`

端口被占。`ss -ltnp | grep 2048` 看是谁，或者 `--port 2049` 换个。

### 账号一直 `auth_required`

先看 `origins`，再看 mtime：

```bash
python3 deploy/check_account.py auth/user@gmail.com
```

`origins=0` → 只能重新隔离登录。
`origins=2` 但 mtime 几小时没动 → 续签停了，看 `journalctl | grep 续签`。

### 一直 429，但账号显示 `ready`

看错误原文里的 `limit:` 那个数字。

```bash
journalctl -u aistudio2api -n 100 | grep -o 'limit: [0-9]*'
```

`limit: 0` → 这个模型在这个通道永久无权限，换通道（Build ↔ Playground）。
`limit: 100` → 今天的额度真用完了。

### 探针吃掉配额

每 5 分钟一次的探针 = 288 次/天。打 100 次/天的模型会占掉一半以上。实测某天 build flash 成功 141 次里 76 次是探针，导致"才 49 次就撞限额"。

**探针要选业务不用的模型**：

```bash
# .env 或 unit 里
WATCHDOG_PROBE_MODEL=gemini-3-flash-preview
```

### 内存看起来够却卡住

Go runtime 只认 `GOGC`，不认 cgroup 的 `MemoryHigh`。到阈值时内核 throttle 分配（进程变慢），但 Go 不会主动 GC。

```bash
Environment=GOMEMLIMIT=900MiB
```

不加这一行，表现是"明明没到 MemoryMax 却突然卡顿"。

---

## 配置速查

| 变量 | 推荐 | 说明 |
|---|---|---|
| `LISTEN_ADDR` | `127.0.0.1:2048` | 只回环，对外走反代 |
| `WAA_BACKEND` | `go` | 纯 Go，不起浏览器，350MB/4账号 |
| `UPSTREAM_CHANNELS` | `build,playground` | 双通道 |
| `BUILD_NATIVE_NONSTREAM` | `true` | 非流式优先 Build；设 false 会慢一个数量级 |
| `PER_ACCOUNT_CONCURRENCY` | `2` | 调高只会更快撞 429 |
| `ROUTING_STRATEGY` | `round-robin` | |
| `REQUEST_TIMEOUT` | `10m` | 默认 30m 偏大，会和反代超时打架 |
| `LOG_MAX_TOTAL_MB` | `200` | |
