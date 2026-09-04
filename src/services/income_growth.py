"""收入增长建议的纯判定层 —— 只吃数字,不碰 DB / FastAPI / HTTP。

职责链接在既有那条线后面:

  - `services/insights.py`         结余基线口径(median / MAD → allocatable)
  - `services/insight_loader.py`   DB 装载 + 分桶
  - `services/goal_plan.py`        结余分配 + 目标可行性
  - 本文件                         「该动收入还是支出」+「每月还差多少」
  - `routers/ai/income_growth.py`  HTTP 组装 + 鉴权 + LLM 建议

## 为什么 lever 跟 diagnosis 是两个字段

`classify_diagnosis`(见 `services/insights.py`)回的是「这个周期发生了什么」;
`lever` 回的是「该动哪一侧」。两者大多数时候同向,但有一种组合只有 lever 表达
得出来:**当期支出超了自己的基线,同时结余率长期偏低**。这时只说
`reduce_spending` 会让用户以为省回基线就够了,而按他自己的基线省到位之后结余率
仍然不达标 —— 这种情况 lever 回 `both`,把两件事都摆出来。

反过来也成立:结余率健康、只是这个月花超了,lever 回 `spending`,**不该**去建议
他增加收入(那是拿一个更难的动作替换一个更简单的动作)。

## gap 的优先级为什么是「目标 → 缓冲 → 地板」

`classify_gap` 四条规则第一条命中即返回,顺序不是随手排的:

1. `goal_shortfall` —— 用户自己设的目标缺口。这是他**亲口说出来**的数,任何
   服务端推断出来的数都不该盖掉它。
2. `buffer_deficit` —— 结余撑不住自己的支出波动(`buffer` 是用户自己月支出的
   MAD)。这是「连自保都做不到」,比攒不够钱更急。
3. `surplus_floor` —— 结余率没到 `_LOW_SAVING_RATE` 这条通用地板线。最弱的一条
   依据(它不来自用户的任何具体意图),所以排最后。
4. 都不成立 → 没有缺口,回 0 + `none`,端点据此跳过 LLM 调用。

## 只回数字和 enum code

同 `services/insights.py`:本仓 locale 只到 zh / zh-CN / zh-TW / en,服务端不
产出文案,`lever` / `gap_basis` 都是 enum code。整个端点里唯一的自然语言是 LLM
生成的 `suggestions`,那部分由端点单独标 `generated`,跟本文件无关。
"""

from __future__ import annotations

from .insights import _LOW_SAVING_RATE, _OVERSPEND_MULT, PeriodTotals, SurplusBaseline

# 阈值一律从 `services/insights.py` 借,不在这里重新写一遍字面量:
#   _OVERSPEND_MULT  = 1.2   当期支出高出自己基线 20% 才算「花超了」
#   _LOW_SAVING_RATE = 0.10  结余率低于 10% 判「问题在收入侧」
# 仓里只能有一套「多少算超支」「多少算结余不够」的口径 —— 复制一份到这里,
# 改了 insights 那边就会出现两个端点给互相矛盾的结论(历史上预算周期算法散成
# 两份就出过这种 bug)。借私有名是有意的:它表达的正是「同一套阈值」。
__all__ = [
    "GAP_BUFFER_DEFICIT",
    "GAP_GOAL_SHORTFALL",
    "GAP_NONE",
    "GAP_SURPLUS_FLOOR",
    "LEVER_BOTH",
    "LEVER_INCOME",
    "LEVER_NONE",
    "LEVER_SPENDING",
    "MAX_SUGGESTIONS",
    "classify_gap",
    "classify_lever",
    "surplus_ratio",
]

# 一次最多回几条建议。5 条已经超过一个人能同时开始的行动数量,再多只是把 token
# 花在用户不会看的第 6 条上。**prompt 侧和响应侧共用这一个数**:prompt 用它约束
# LLM(`prompts.build_income_growth_messages`),端点用它截断实际返回值(LLM 不
# 听话时兜住)。两处各写一个字面量迟早会分叉,那时用户会静默丢掉第 5 条建议。
MAX_SUGGESTIONS = 5

# 金额比较容差 = 半分钱,跟 `services/goal_plan.py::_MONEY_EPSILON` 同一个数。
# 响应边界按 2 位小数取整,比这更小的残额是浮点噪声,不该让 `gap_basis` 在
# 1e-12 的目标缺口上从 `buffer_deficit` 翻成 `goal_shortfall`。
_MONEY_EPSILON = 0.005

LEVER_INCOME = "income"
LEVER_SPENDING = "spending"
LEVER_BOTH = "both"
LEVER_NONE = "none"

GAP_GOAL_SHORTFALL = "goal_shortfall"
GAP_BUFFER_DEFICIT = "buffer_deficit"
GAP_SURPLUS_FLOOR = "surplus_floor"
GAP_NONE = "none"


def surplus_ratio(median_income: float, median_surplus: float) -> float:
    """结余率 = `median_surplus / median_income`,收入 <= 0 时回 0.0。

    不抛 ZeroDivisionError:没有收入记录的账本(新用户 / 只记支出的用户)是
    正常状态,不该让端点 500。回 0.0 而不是 None,是因为下游只做阈值比较 ——
    「没有收入」在「结余率够不够」这个问题上等价于「结余率为 0」。
    """
    if median_income <= 0.0:
        return 0.0
    return median_surplus / median_income


def _is_overspending(baseline: SurplusBaseline, current: PeriodTotals) -> bool:
    """当期支出是否高出自己基线 `_OVERSPEND_MULT` 倍 —— 同 `classify_diagnosis`。"""
    return current.expense > baseline.median_expense * _OVERSPEND_MULT


def classify_lever(
    baseline: SurplusBaseline,
    current: PeriodTotals,
    ratio: float,
    diagnosis: str,
) -> str:
    """回 `income` / `spending` / `both` / `none` 之一。

    - `none`      样本不足(`diagnosis == "insufficient_data"`),或支出没超基线
                  且结余率达标 —— 没有需要动的那一侧。
    - `both`      当期超支 **且** 结余率偏低:省回基线也攒不下钱,两件事都得动。
    - `income`    支出正常,结余率偏低 —— 该动的是收入侧(见 `insights.py` 里
                  `increase_income` 那条注释:此时建议「再省点」是错误建议)。
    - `spending`  结余率健康,只是这个周期花超了 —— 动支出侧就够了。

    `diagnosis` 由调用方从 `classify_diagnosis` 取,不在这里重算:同一个窗口
    只能有一套诊断口径。
    """
    if diagnosis == "insufficient_data":
        return LEVER_NONE
    overspending = _is_overspending(baseline, current)
    thin_surplus = ratio < _LOW_SAVING_RATE
    if overspending and thin_surplus:
        return LEVER_BOTH
    if thin_surplus:
        return LEVER_INCOME
    if overspending:
        return LEVER_SPENDING
    return LEVER_NONE


def classify_gap(
    baseline: SurplusBaseline,
    ratio: float,
    goal_shortfall_total: float,
) -> tuple[float, str]:
    """回 `(每月还差多少, gap_basis)`,四条规则第一条命中即返回。

    优先级理由见模块 docstring。所有分支的金额都夹在 `max(0.0, ...)`:缺口按
    定义不为负,回负数只会让前端去猜「负缺口」是什么意思。

    `goal_shortfall_total` 由调用方用 `services/goal_plan.py` 那条链算好传进来
    (`allocate` + `required_monthly` 的逐目标 shortfall 求和),本文件不碰 DB,
    也不重新推导 allocatable —— 仓里只能有一套结余分配口径。
    """
    if goal_shortfall_total > _MONEY_EPSILON:
        return max(0.0, goal_shortfall_total), GAP_GOAL_SHORTFALL
    if baseline.buffer - baseline.median_surplus > _MONEY_EPSILON:
        return max(0.0, baseline.buffer - baseline.median_surplus), GAP_BUFFER_DEFICIT
    if ratio < _LOW_SAVING_RATE and baseline.median_income > 0.0:
        floor_amount = _LOW_SAVING_RATE * baseline.median_income - baseline.median_surplus
        return max(0.0, floor_amount), GAP_SURPLUS_FLOOR
    return 0.0, GAP_NONE
