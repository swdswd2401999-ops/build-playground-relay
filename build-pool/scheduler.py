"""
Build 多账号调度器（参考实现）

目标：多个 build executor（每个对应一个 Google 账号）之间自动分流，
      一个号耗尽/报错就顺到下一个，对上游客户端完全透明。

关键前提（本项目实测结论）：
  - 站端拿不到 Google 账号的额度余额，只有 executor 自己能读页面里的 quota。
  - 所以必须由 executor 主动上报额度快照；没有快照就只能靠 429 事后兜底。
  - 每个 executor 的并发上限是 1（Playground 的 proof 绑输入框，必须串行；
    Build 侧建议同样按 1 处理，避免同一账号内互相打架）。

调度经济学（比"用完一个再用下一个"更优）：
  优先消耗「最早重置」的额度 —— 先把即将过期的那份用掉，账面上总额度不变，
  但浪费掉的过期额度最少。同重置时间时，余量少的先用（尽早清空，腾出干净状态）。

用法：
    python scheduler.py            # 跑自检
"""
from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

READY, DRAINING, COOLING, DEAD = "ready", "draining", "cooling", "dead"
DRAIN_THRESHOLD = 0.05          # 余量低于 5% 不再接新活
MAX_INFLIGHT = 1                # 每账号并发上限
SHORT_COOLDOWN = 30.0           # 5xx / 网络错误退避
DEAD_CODES = {401, 403}         # 凭证级失效
QUOTA_CODES = {429}             # 额度耗尽


@dataclass
class Executor:
    eid: str
    account: str                      # 脱敏标识，如 a***@example.com
    models: Set[str] = field(default_factory=set)
    state: str = READY
    remaining: Dict[str, float] = field(default_factory=dict)   # family -> 0..1
    reset_at: Dict[str, float] = field(default_factory=dict)    # family -> unix ts
    inflight: int = 0
    fail_streak: int = 0
    cooldown_until: float = 0.0
    served: int = 0
    last_used: float = 0.0

    def family(self, model: str) -> str:
        """build-gemini-3.7-flash -> gemini-3.7-flash 家族键；按需扩展。"""
        return model.split("/")[-1].removeprefix("build-")

    def usable(self, model: str, now: float) -> bool:
        if self.state in (DEAD, DRAINING):
            return False
        if self.state == COOLING and now < self.cooldown_until:
            return False
        if self.inflight >= MAX_INFLIGHT:
            return False
        fam = self.family(model)
        if self.models and not any(fam.startswith(m) or m.startswith(fam) for m in self.models):
            return False
        rem = self.remaining.get(fam)
        if rem is not None and rem <= 0:
            return False
        return True

    def score(self, model: str) -> tuple:
        """越小越优先：先耗尽最早重置的额度。"""
        fam = self.family(model)
        return (self.reset_at.get(fam, float("inf")), self.remaining.get(fam, 0.5), self.inflight, self.served)


class Pool:
    def __init__(self) -> None:
        self.executors: Dict[str, Executor] = {}

    # ---- executor 生命周期 ----
    def register(self, eid: str, account: str, models: Iterable[str] = ()) -> Executor:
        ex = Executor(eid=eid, account=account, models=set(models))
        self.executors[eid] = ex
        return ex

    def unregister(self, eid: str) -> None:
        self.executors.pop(eid, None)

    def report_quota(self, eid: str, family: str, remaining: float, reset_at: float) -> None:
        """executor 上报额度快照 —— 这是整套调度成立的关键输入。"""
        ex = self.executors[eid]
        ex.remaining[family] = max(0.0, min(1.0, remaining))
        ex.reset_at[family] = reset_at
        if ex.state == COOLING and remaining > 0:
            ex.state, ex.cooldown_until = READY, 0.0
        if ex.state in (READY, DRAINING):
            ex.state = DRAINING if ex.remaining[family] <= DRAIN_THRESHOLD else READY

    # ---- 调度 ----
    def pick(self, model: str, now: Optional[float] = None) -> Optional[Executor]:
        now = time.time() if now is None else now
        cands = [e for e in self.executors.values() if e.usable(model, now)]
        if not cands:
            return None
        return min(cands, key=lambda e: e.score(model))

    def acquire(self, model: str, now: Optional[float] = None) -> Optional[Executor]:
        ex = self.pick(model, now)
        if ex:
            ex.inflight += 1
            ex.last_used = time.time() if now is None else now
        return ex

    def release(self, eid: str, model: str, ok: bool, status: int = 200,
                now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        ex = self.executors[eid]
        ex.inflight = max(0, ex.inflight - 1)
        fam = ex.family(model)
        if ok:
            ex.fail_streak = 0
            ex.served += 1
            ex.state = READY
            if fam in ex.remaining:
                ex.remaining[fam] = max(0.0, ex.remaining[fam] - 0.02)  # 无快照时的粗略估计
                if ex.remaining[fam] <= DRAIN_THRESHOLD:
                    ex.state = DRAINING
            return
        ex.fail_streak += 1
        if status in DEAD_CODES:
            ex.state = DEAD
        elif status in QUOTA_CODES:
            ex.remaining[fam] = 0.0
            ex.state = COOLING
            ex.cooldown_until = max(ex.reset_at.get(fam, now + 300), now + 60)
        else:
            ex.state = COOLING
            ex.cooldown_until = now + SHORT_COOLDOWN * min(ex.fail_streak, 4)

    def snapshot(self, now: Optional[float] = None) -> List[dict]:
        now = time.time() if now is None else now
        return [
            {"eid": e.eid, "acct": e.account, "state": e.state, "served": e.served,
             "inflight": e.inflight, "rem": {k: round(v, 3) for k, v in e.remaining.items()},
             "reset_in": {k: f"{(v - now) / 3600:.2f}h" for k, v in e.reset_at.items()}}
            for e in sorted(self.executors.values(), key=lambda x: x.eid)
        ]


# ---------------------------- 自检 ----------------------------
def _selftest() -> None:
    now = 1_000_000.0
    pool = Pool()
    H = 3600
    # 三个账号：A 余量 8%、5h 后重置；B 满额、3h 后重置；C 满额、24h 后重置
    pool.register("E1", "acct-a***", {"gemini-3.7-flash"})
    pool.register("E2", "acct-b***", {"gemini-3.7-flash"})
    pool.register("E3", "acct-c***", {"gemini-3.7-flash"})
    pool.report_quota("E1", "gemini-3.7-flash", 0.08, now + 5 * H)
    pool.report_quota("E2", "gemini-3.7-flash", 1.00, now + 3 * H)
    pool.report_quota("E3", "gemini-3.7-flash", 1.00, now + 24 * H)

    print("初始状态:")
    for r in pool.snapshot(now):
        print("  ", r)

    print("\n请求序列（先耗尽最早重置的额度；第 2 次打 429）:")
    order, t = [], now
    for i in range(6):
        ex = pool.acquire("build-gemini-3.7-flash", t)
        if ex is None:
            print(f"  #{i+1} 无可用 executor（全部冷却/耗尽）")
            t += 1
            continue
        order.append(ex.eid)
        ok = i != 1
        print(f"  #{i+1} -> {ex.eid} {ex.account} rem={ex.remaining.get('gemini-3.7-flash'):.2f} "
              f"{'200' if ok else '429'}")
        pool.release(ex.eid, "build-gemini-3.7-flash", ok=ok, status=200 if ok else 429, now=t)
        t += 1

    print("\n调度顺序:", order)
    print("\n最终状态:")
    for r in pool.snapshot(t):
        print("  ", r)

    # 经济学断言
    assert order[0] == "E2", f"应优先用最早重置(3h)的号，实际 {order}"
    assert order[2] == "E1", f"E2 429 后应顺延到 E1，实际 {order}"
    assert "E2" not in order[2:], f"E2 冷却期间不应再被派活，实际 {order}"
    assert pool.executors["E2"].state == COOLING
    assert pool.executors["E1"].state == DRAINING, "余量跌破阈值的号应停止接新活"
    assert order[4:] == ["E3", "E3"], f"E1 进入 draining 后才轮到 E3，实际 {order}"

    # 恢复断言：E2 重置后上报满额 → 重新参与调度
    pool.report_quota("E2", "gemini-3.7-flash", 1.0, now + 48 * H)
    assert pool.executors["E2"].state == READY
    nxt = pool.acquire("build-gemini-3.7-flash", t + 1)
    print(f"\nE2 重置恢复后下一个: {nxt.eid if nxt else 'None'}")
    print("\n断言全部通过。")


if __name__ == "__main__":
    _selftest()
