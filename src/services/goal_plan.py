"""攒钱目标的可行性 + 结余分配纯计算层 —— 只吃数字,不碰 DB / FastAPI / HTTP。

职责链跟 `services/insights.py` 那条线接在一起:

  - `services/insights.py`        结余基线口径(median / MAD → allocatable)
  - `services/insight_loader.py`  DB 装载 + 分桶
  - 本文件                        把 allocatable 分给目标 + 判定每个目标可行性
  - `routers/goals.py`            HTTP 组装 + 鉴权 + 序列化

## 为什么按权重分,而不是按「缺口」分

按缺口(required_monthly)按比例分,等于让「最不可能达成的目标」自动吃掉最多
预算 —— 一个 30 万的买房目标会把 3000 元的换手机目标饿死,而用户的真实优先
级信息(`priority`)被完全忽略。所以分配只看用户自己填的权重,可行性单独回
一个 enum code,让用户自己决定要不要调权重 / 调 deadline / 调金额。

## 只回数字和 enum code

同 `services/insights.py`:本仓 locale 只到 zh / zh-CN / zh-TW / en
(`routers/ai/ask.py:56`),服务端不产出文案。`feasibility` 回 4 个 code,
「延后 deadline / 加钱 / 降目标」这三个选项由前端拿 `required_monthly`、
`allocated_monthly`、`shortfall` 自己组装。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

# 可行性判定阈值。
#   _TIGHT_RATIO = 0.9  分到的钱够用,但余量不足 10% 就判 `tight`:这种目标
#     一次意外支出就会脱轨,跟真正宽裕的 `feasible` 混成一档会给出虚假的安全
#     感。取 0.9 与 `routers/read/workspace.py::_ANOMALY_DEVIATION_MULT`(1.2)
#     是同一档人工阈值量级 —— 比 1.0 松一点,又不至于把「刚刚够」全报成紧张。
_TIGHT_RATIO = 0.9

# 金额比较容差 = 半分钱。响应边界按 2 位小数取整,比这更小的残额是浮点噪声:
#   - 不该让 `feasible` / `infeasible` 在 1000.0 vs 1000.0000000001 上翻转;
#   - 不该让重分配循环为了 1e-12 的剩余池子再转一圈。
_MONEY_EPSILON = 0.005

FEASIBILITY_FEASIBLE = "feasible"
FEASIBILITY_TIGHT = "tight"
FEASIBILITY_INFEASIBLE = "infeasible"
FEASIBILITY_NO_DEADLINE = "no_deadline"


@dataclass(frozen=True)
class GoalDemand:
    """分配算法需要的最小输入 —— 一个目标只贡献「还差多少」和「权重多大」。

    不吃 ORM 对象:纯值进纯值出,单测不用起 app / 建表 / 造 token。
    """

    goal_id: str
    remaining_amount: float
    priority: int


@dataclass(frozen=True)
class AllocationResult:
    by_goal: Mapping[str, float]
    # 分不出去的部分(所有目标都封顶后剩下的),不藏起来:用户看到「还有钱没
    # 安排」才知道可以加目标或提高某个目标的金额。
    unallocated: float


def months_remaining(deadline: date | None, today: date) -> int | None:
    """从 `today` 到 `deadline` 还剩几个**整**月,下限 0;没有 deadline 回 None。

    取整月(不是 `ceil`)是有意的:剩 25 天的目标算 0 个月,配合
    `required_monthly` 的 `months <= 0` 分支直接判「本月内要全额到位」。向上取整
    会把这种目标说成「还有一个月,每月只要一半」,而那个月并不存在。

    按「日」比较判整月:`deadline.day < today.day` 则减一个月。1 月 31 日 →
    2 月 28 日这类月末边界会算成 0 个月(偏保守一天),不为此引入 relativedelta
    这类依赖 —— 差一天不改变任何判定档位。
    """
    if deadline is None:
        return None
    months = (deadline.year - today.year) * 12 + (deadline.month - today.month)
    if deadline.day < today.day:
        months -= 1
    return max(0, months)


def required_monthly(remaining_amount: float, months: int | None) -> float | None:
    """达成 deadline 所需的月供。没有 deadline 回 None(无期限目标没有「必须」)。

    `months == 0` 且还差钱 → 回整个缺口:钱是这个月就要到位的,不做除零。
    """
    if months is None:
        return None
    if remaining_amount <= 0.0:
        return 0.0
    if months <= 0:
        return remaining_amount
    return remaining_amount / months


def projected_months(remaining_amount: float, allocated_monthly: float) -> int | None:
    """按当前分配额,还要几个月攒满。分配额为 0 时回 None(永远攒不满)。"""
    if allocated_monthly <= 0.0:
        return None
    if remaining_amount <= 0.0:
        return 0
    return math.ceil(remaining_amount / allocated_monthly)


def classify_feasibility(
    remaining_amount: float,
    required: float | None,
    allocated_monthly: float,
) -> str:
    """回 4 个 enum code 之一,措辞交前端(见模块 docstring)。

    - `feasible`     已攒满(`remaining_amount <= 0`),或分配额比所需月供还有
                     富余(超出 `_TIGHT_RATIO` 那条线)。
    - `no_deadline`  目标没有 deadline,没有「所需月供」可比,不判可行性。
    - `infeasible`   分配额低于所需月供 —— 按当前基线和权重,这个 deadline 到不了。
    - `tight`        够,但余量不足:`required >= allocated * _TIGHT_RATIO`。
    """
    if remaining_amount <= 0.0:
        # 已经攒满的目标是终态,不参与分配也就没有紧张与否
        return FEASIBILITY_FEASIBLE
    if required is None:
        return FEASIBILITY_NO_DEADLINE
    if allocated_monthly < required - _MONEY_EPSILON:
        return FEASIBILITY_INFEASIBLE
    if required >= allocated_monthly * _TIGHT_RATIO:
        return FEASIBILITY_TIGHT
    return FEASIBILITY_FEASIBLE


def allocate(demands: Sequence[GoalDemand], allocatable: float) -> AllocationResult:
    """把 `allocatable` 按 `priority` 权重分给各目标,单个目标不超过它的缺口。

    规则:

    - `remaining_amount <= 0` 的目标不参与:已经攒满了,再给它预算是浪费额度。
    - 权重全为 0 时平均分 —— 用户没表达偏好,服务端不替他猜一个。
    - 混合情况(有人填了权重有人没填)先按权重走,权重 0 的这一轮分到 0。但等
      所有正权重目标都封顶之后,剩下的候选集就是「全 0 权重」,于是按上一条平
      分给它们:钱与其停在 `unallocated`,不如落到一个还差钱的活动目标上。
    - 单个目标封顶在自己的缺口,溢出的额度重分配给还没封顶的目标;全部封顶后
      仍有剩余进 `unallocated`。
    - 循环轮次硬上界 `len(demands) + 1`:每一轮要么把池子花光(下一轮开头即
      退出),要么至少有一个目标被封顶移出候选集,所以这个上界一定够 —— 浮点
      残额也不可能把它转成死循环。

    输入顺序决定同权重下的遍历顺序,同样输入必给同样输出。
    """
    allocated: dict[str, float] = {d.goal_id: 0.0 for d in demands}
    pool = max(0.0, allocatable)
    open_demands = [d for d in demands if d.remaining_amount > 0.0]

    for _ in range(len(demands) + 1):
        if pool <= _MONEY_EPSILON or not open_demands:
            break
        total_weight = float(sum(d.priority for d in open_demands))
        share_count = len(open_demands)
        still_open: list[GoalDemand] = []
        given_total = 0.0
        for demand in open_demands:
            share = (
                pool * (demand.priority / total_weight)
                if total_weight > 0.0
                else pool / share_count
            )
            headroom = demand.remaining_amount - allocated[demand.goal_id]
            given = min(share, headroom)
            allocated[demand.goal_id] += given
            given_total += given
            if headroom - given > _MONEY_EPSILON:
                still_open.append(demand)
        pool = max(0.0, pool - given_total)
        if given_total <= _MONEY_EPSILON and len(still_open) == len(open_demands):
            # 这一轮既没分出钱、也没封顶任何目标 —— 再转也只是原地空跑
            break
        open_demands = still_open

    return AllocationResult(by_goal=allocated, unallocated=pool)


__all__ = [
    "FEASIBILITY_FEASIBLE",
    "FEASIBILITY_INFEASIBLE",
    "FEASIBILITY_NO_DEADLINE",
    "FEASIBILITY_TIGHT",
    "AllocationResult",
    "GoalDemand",
    "allocate",
    "classify_feasibility",
    "months_remaining",
    "projected_months",
    "required_monthly",
]
