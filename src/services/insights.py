"""财务洞察的纯计算层 —— 只吃数字,不碰 DB / FastAPI / HTTP。

放在 `services/` 而不是 `routers/read/` 里的原因:这里全是能单独喂数据跑的
统计口径,单测不用起 app、不用建表、不用造 token。三层职责分开:

  - 本文件                                    统计口径(纯函数 + dataclass)
  - `routers/read/_shared.py::_load_insight_window`   DB 装载 + 分桶
  - `routers/read/insights.py`                       HTTP 组装 + 鉴权

## buffer 为什么是「用户自己的 MAD」而不是固定百分比

最初的功能设想是把结余按固定的 70/20/10 分掉。那跟它想取代的 50/30/20 是
同一个错误:一个模板套所有人。同样是月结余 2000 元,

  - 月支出稳定在 ±100 元的人,留 600 元缓冲纯属浪费;
  - 月支出在 3000~9000 元之间跳的人,留 600 元缓冲下个月就穿。

所以 buffer 取用户自己月支出的 MAD(中位绝对偏差),回答的是「你自己的波动
有多大」:稳定的人 buffer 自然小,波动大的人 buffer 自然大,不需要任何人工
挑的百分比。用 MAD 而不是标准差,是因为样本只有 3-24 个月,一次装修或买车
就能把 σ 拉飞,而中位数系的统计量对这种单点离群值不敏感。

## 只回数字和 enum code

本仓的 locale 只支持 zh / zh-CN / zh-TW / en(见 `routers/ai/ask.py`),没有
其它语种可落地,所以诊断结论只回 4 个 enum code,措辞由前端按自己的 locale
决定。服务端不产出文案。
"""

from __future__ import annotations

import statistics as _stats
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

# 诊断阈值。跟 `routers/read/workspace.py` 的 anomaly 判定对齐,避免一个仓里
# 存在两套「多少算异常」的口径:
#   _DIAGNOSIS_MIN_PERIODS = 3  对齐 _ANOMALY_MIN_MONTHS。1-2 个周期的中位数
#     没有统计意义(2 个样本的 median 就是均值),一次装修就能把基线带偏。
#   _OVERSPEND_MULT = 1.2       对齐 _ANOMALY_DEVIATION_MULT。高出自己基线
#     20% 才算「花超了」;10% 以内是正常月度抖动,报了就是狼来了。
#   _LOW_SAVING_RATE = 0.10     结余率低于 10% 判「问题在收入侧」。这是攒得
#     出应急金的常见下限档;而且走到这一判定时支出并没有超自己的基线,继续
#     建议「再省点」是错的 —— 该动的是收入。
_DIAGNOSIS_MIN_PERIODS = 3
_OVERSPEND_MULT = 1.2
_LOW_SAVING_RATE = 0.10


@dataclass(frozen=True)
class PeriodTotals:
    """一个预算周期的收支合计。

    `bucket` 是「周期标签月」(`%Y-%m`),跟 `_bucket_key(scope="year", ...)`
    同一口径 —— month_start_day=10 时 `2026-06` 表示 2026-06-10 ~ 2026-07-10。
    """

    bucket: str
    income: float
    expense: float

    @property
    def surplus(self) -> float:
        return self.income - self.expense


@dataclass(frozen=True)
class InsightWindow:
    """`_load_insight_window` 的返回值 —— 一个跨周期窗口的全部原料。

    `periods` 只含**已完成**周期(升序),当前没走完的周期单独放 `current`。
    理由:月中拿一个只过了 3 天的周期参与 median,会把每条基线都往下拽(3 天
    的支出当一个月用),用户月初看到的「基线」会比月末低一大截。

    `periods` 对窗口内没有任何交易的周期补 0 —— 空周期是真的没花钱,不补 0
    的话 median / MAD 只反映「有记账的月」,基线系统性偏高。

    `category_by_period` 是 `{周期标签: {分类名: 支出}}`,含 `current` 那个桶;
    要不要把当前周期算进分类基线,由调用方通过 `compute_category_baselines`
    的 `buckets` 参数决定。

    `restricted_to_creator=True` 表示这些数字只统计了调用者自己记的账(共享
    账本场景),跟同一账本的「全员合计」不是一个量,必须透出给前端。
    """

    periods: tuple[PeriodTotals, ...]
    category_by_period: Mapping[str, Mapping[str, float]]
    start_at: datetime
    end_at: datetime
    restricted_to_creator: bool
    current: PeriodTotals


def median_or_zero(values: Sequence[float]) -> float:
    """空序列返 0.0。`statistics.median` 空输入抛 `StatisticsError`,读端点不该
    因为「这个账本还没记过账」变成 500。"""
    if not values:
        return 0.0
    return float(_stats.median(values))


def mad(values: Sequence[float]) -> float:
    """中位绝对偏差 `median(|v - median(v)|)`。

    抗离群:3-24 个月的样本里一次装修就能把标准差拉飞,MAD 不会。
    """
    if not values:
        return 0.0
    center = median_or_zero(values)
    return float(_stats.median([abs(v - center) for v in values]))


@dataclass(frozen=True)
class SurplusBaseline:
    median_income: float
    median_expense: float
    median_surplus: float
    buffer: float
    allocatable: float
    basis_periods: int


def compute_surplus_baseline(periods: Sequence[PeriodTotals]) -> SurplusBaseline:
    """从已完成周期算结余基线。

    `median_surplus` 取「逐周期结余的中位数」,不是 `median_income -
    median_expense` —— 后者会把收入最高的月和支出最低的月拼成一个从未存在过
    的月份,给出用户永远达不到的结余。

    `allocatable = max(0, median_surplus - buffer)`:能拿去分配的只有「稳定
    结余扣掉自己的波动」那部分,下限 0(结余撑不起波动时无可分配额度,不返
    负数让前端去猜)。
    """
    expenses = [p.expense for p in periods]
    median_surplus = median_or_zero([p.surplus for p in periods])
    buffer = mad(expenses)
    return SurplusBaseline(
        median_income=median_or_zero([p.income for p in periods]),
        median_expense=median_or_zero(expenses),
        median_surplus=median_surplus,
        buffer=buffer,
        allocatable=max(0.0, median_surplus - buffer),
        basis_periods=len(periods),
    )


@dataclass(frozen=True)
class CategoryBaseline:
    category_name: str
    median: float
    mean: float
    periods_present: int


def compute_category_baselines(
    category_by_period: Mapping[str, Mapping[str, float]],
    buckets: Sequence[str],
) -> list[CategoryBaseline]:
    """按分类算跨周期支出基线,只看 `buckets` 列出的周期。

    某周期里分类没出现 → 该周期计 0.0(那个月真的没在这个分类上花钱,不是缺
    数据),median 因此会被 0 拉下来 —— 这是想要的:一年只买两次的分类不该
    拿到 12 个月的高基线。`periods_present` 单独回「出现过几个周期」,让调用
    方能把「每月都有」和「偶发大额」区分开。

    只有在 `buckets` 里出现过的分类才进结果 —— 只在当前未完成周期出现过的
    新分类,基线会是 0,回一条 0 元建议没有意义。

    排序:median 降序,同 median 按名字升序 —— 同样输入必须给同样顺序。
    """
    names: set[str] = set()
    for bucket in buckets:
        names.update(category_by_period.get(bucket, {}))

    out: list[CategoryBaseline] = []
    for name in sorted(names):
        series = [float(category_by_period.get(b, {}).get(name, 0.0)) for b in buckets]
        out.append(
            CategoryBaseline(
                category_name=name,
                median=median_or_zero(series),
                mean=(sum(series) / len(series)) if series else 0.0,
                periods_present=sum(1 for b in buckets if name in category_by_period.get(b, {})),
            )
        )
    out.sort(key=lambda c: (-c.median, c.category_name))
    return out


def compute_safe_daily(remaining: float, days_remaining: int) -> float:
    """剩余额度摊到剩余天数的「日均还能花」。

    `days_remaining <= 0`(周期最后一天)时退化成「今天还能花多少」,不做
    除法 —— 周期边界上的 0 除会把读端点打成 500。
    """
    if days_remaining <= 0:
        return max(0.0, remaining)
    return max(0.0, remaining / days_remaining)


def classify_diagnosis(baseline: SurplusBaseline, current: PeriodTotals) -> str:
    """回 4 个 enum code 之一,前端自己配文案(见模块 docstring)。

    - `insufficient_data` 样本不足 `_DIAGNOSIS_MIN_PERIODS` 个周期,或基线
      收入 <= 0(没有收入记录时任何结余率都是噪声)。
    - `reduce_spending`   当前周期支出高出自己基线 `_OVERSPEND_MULT` 倍。
    - `increase_income`   支出没超基线,但结余率仍低于 `_LOW_SAVING_RATE`。
    - `on_track`          以上都不成立。
    """
    if baseline.basis_periods < _DIAGNOSIS_MIN_PERIODS or baseline.median_income <= 0:
        return "insufficient_data"
    if current.expense > baseline.median_expense * _OVERSPEND_MULT:
        return "reduce_spending"
    # 走到这里说明当前支出没超自己基线的 _OVERSPEND_MULT 倍 —— 支出侧没毛病,
    # 结余率却还是上不去,那问题在收入侧。此时建议「再省点」是错误建议。
    # median_income > 0 由上面第一个分支保证,这里的除法安全。
    if baseline.median_surplus / baseline.median_income < _LOW_SAVING_RATE:
        return "increase_income"
    return "on_track"
