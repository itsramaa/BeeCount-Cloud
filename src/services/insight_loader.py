"""财务洞察的 DB 装载层 —— 把 projection 里的行读成 `services/insights.py` 能吃的数字。

三层职责(见 `services/insights.py` 模块 docstring):

  - `services/insights.py`      统计口径(纯函数,不碰 DB)
  - 本文件                       DB 装载 + 分桶(碰 DB,不碰 HTTP)
  - `routers/insights.py`       HTTP 组装 + 鉴权

周期口径全部复用既有 helper,不新写第四套月周期实现:
`_current_period_range`(当前周期边界,与 `/read/.../budgets/usage` 同一函数)、
`_bucket_key(scope="year", ...)`(周期标签月 `%Y-%m`)、`_clamp_month_start_day`。

从 `services/` 反向 import `routers/read/*` 是有意的:这些 helper 是账本周期的
唯一权威实现,复制一份到 services 下才是真问题(历史上 budget 周期算法散成两份
就出过对不上的 bug)。read 侧要 import 本模块时用函数内 import(见 `list_budgets_usage`),避免成环。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import false as sa_false
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ledger_access import count_ledger_members
from ..models import (
    Ledger,
    ReadBudgetProjection,
    ReadTxProjection,
    UserCategoryProjection,
)
from ..routers.read._shared import _bucket_key, _clamp_month_start_day
from ..routers.read.ledgers import _current_period_range
from .insights import InsightWindow, PeriodTotals

_SECONDS_PER_DAY = 86400


def _as_utc(value: datetime) -> datetime:
    """SQLite 存的是 naive datetime,比较前统一补 UTC 标记(同 `_shared._to_utc`)。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _shift_months(moment: datetime, months: int) -> datetime:
    """按月平移,日期部分不动。

    只对 `_current_period_range` 的返回值用:那里的 day 已经钳到 [1, 28],所以
    `replace(year=..., month=...)` 不会碰到 2 月 29/30/31 的坑。
    """
    total = moment.year * 12 + (moment.month - 1) + months
    return moment.replace(year=total // 12, month=total % 12 + 1)


def period_progress(start: datetime, end: datetime, now: datetime) -> tuple[int, int]:
    """回 `(days_total, days_remaining)`。

    `days_total` 是周期总天数(28-31,自定义起始日下不是固定 30);
    `days_remaining` 是「从现在到周期结束还剩几个整天」,下限 0 —— 周期最后一天
    剩余不足 1 天时回 0,由 `compute_safe_daily` 退化成「今天还能花多少」。
    """
    total_seconds = (_as_utc(end) - _as_utc(start)).total_seconds()
    days_total = max(1, int(round(total_seconds / _SECONDS_PER_DAY)))
    remaining_seconds = (_as_utc(end) - _as_utc(now)).total_seconds()
    days_remaining = max(0, int(remaining_seconds // _SECONDS_PER_DAY))
    return days_total, days_remaining


def budget_usage_rows(
    db: Session,
    *,
    ledger: Ledger,
    start: datetime,
    end: datetime,
) -> list[tuple[ReadBudgetProjection, float]]:
    """每个 budget 在 `[start, end)` 内的已用金额,回 `[(budget_row, used), ...]`。

    **预算用量聚合的唯一实现。** 另一个调用方是
    `routers/read/ledgers.py::list_budgets_usage`(它只解析周期边界 + 组装
    响应),所以改这里等于改 `/budgets/usage` 的返回值。

    口径,一条都不能改:
    - 不 filter `enabled` —— 跟 `list_budgets` 对齐,前端 join 时不丢 budget。
    - `(budget_type, category_sync_id)` 维度去重,`sync_id` 字典序最大胜出;
      `category` 预算缺 `category_sync_id` 的是脏数据,直接跳过。
    - 金额折账本本位币:`COALESCE(native_amount, amount)`(0018 账本维度口径)。
    - `category` 预算连带统计所有 `parent_sync_id` 指向它的子分类;子分类按
      `ledger.user_id` 查(user-global 表),不是 caller。
    - **只看 `exclude_from_budget`,不看 `exclude_from_stats`** —— 两标记独立
      (设计 D2):`exclude_from_stats=True, exclude_from_budget=False` 的交易
      不进收支统计但仍算预算用量,`tests/test_budget_usage_exclude_flags.py`
      锁了这个行为。
    - 回 `abs(used)`。
    """
    raw = db.scalars(
        select(ReadBudgetProjection).where(
            ReadBudgetProjection.ledger_id == ledger.id,
        )
    ).all()

    dedup: dict[tuple[str, str], ReadBudgetProjection] = {}
    for b in raw:
        btype = b.budget_type or "total"
        if btype == "category" and not b.category_sync_id:
            continue
        key = (btype, b.category_sync_id or "")
        current = dedup.get(key)
        if current is None or current.sync_id < b.sync_id:
            dedup[key] = b

    out: list[tuple[ReadBudgetProjection, float]] = []
    for b in dedup.values():
        base_q = select(
            func.coalesce(
                func.sum(func.coalesce(ReadTxProjection.native_amount, ReadTxProjection.amount)),
                0.0,
            )
        ).where(
            ReadTxProjection.ledger_id == ledger.id,
            ReadTxProjection.tx_type == "expense",
            ReadTxProjection.happened_at >= start,
            ReadTxProjection.happened_at < end,
            ReadTxProjection.exclude_from_budget == sa_false(),
        )
        if (b.budget_type or "total") == "category" and b.category_sync_id:
            child_ids = list(
                db.scalars(
                    select(UserCategoryProjection.sync_id).where(
                        UserCategoryProjection.user_id == ledger.user_id,
                        UserCategoryProjection.parent_sync_id == b.category_sync_id,
                    )
                ).all()
            )
            ids = [b.category_sync_id, *child_ids]
            base_q = base_q.where(ReadTxProjection.category_sync_id.in_(ids))

        used = float(db.scalar(base_q) or 0.0)
        out.append((b, abs(used)))
    return out


def load_insight_window(
    db: Session,
    *,
    ledger: Ledger,
    user_id: str,
    lookback_periods: int,
    tz_offset_minutes: int,
) -> InsightWindow:
    """装载一个跨周期洞察窗口。

    窗口 = `lookback_periods` 个预算周期,**以当前周期的结束为窗口结束**,所以
    最后一个周期就是当前那个没走完的周期 → 进 `current`,不进 `periods`。
    基线因此建立在 `lookback_periods - 1` 个已完成周期上(即 `basis_periods`)。
    月中的半个周期参与 median 会把每条基线往下拽(3 天的支出当一个月用),所以
    宁可少一个样本也不能混进去。

    周期标签用 `_bucket_key(scope="year", ...)` 的「周期标签月」口径:
    month_start_day=10 时 `2026-06` 指本地 2026-06-10 ~ 2026-07-10。

    ## 一次 SELECT + Python 分桶

    参照 `routers/read/workspace.py::workspace_analytics` 的做法:行扫 + Python
    累加,不用 SQL `GROUP BY`。原因是 month_start_day 分桶要按用户本地时区折算,
    SQLite 和 PostgreSQL 的日期函数写法不通用。
    **上限**:普通账本(几千到几万笔)没问题,超大账本(十万笔以上)这里会成为
    热点,届时该改成按周期边界发 N 条聚合 SQL,或者加物化的月度汇总表。

    ## 共享账本归属过滤(不要简化)

    `restricted_to_creator = 成员数 > 1`。只有多成员账本才按
    `created_by_user_id == user_id` 过滤;**单成员账本一律全量统计,不看
    created_by_user_id**。

    理由:`src/projection.py:258-260` 的 `created_by_user_id` 取
    `existing_creator or payload_creator or updatedByUserId`,而
    `src/routers/sync/push.py:275-287` 是共享账本修复之后才开始兜底注入
    `updatedByUserId` 的 —— 老服务端写入的行这一列可能是 NULL。单成员账本上
    按 created_by_user_id 过滤会把这些老行全滤掉,给出一个「你这几个月零支出」
    的自信错数,而用户会照着它做决定。
    """
    now = datetime.now(timezone.utc)
    month_start_day = _clamp_month_start_day(ledger.month_start_day)
    current_start, current_end = _current_period_range(month_start_day, now)

    # 窗口内各周期的起点(升序),最后一个是当前周期。
    starts = [
        _shift_months(current_start, -offset) for offset in range(lookback_periods - 1, -1, -1)
    ]
    window_start, window_end = starts[0], current_end

    # 标签也走 _bucket_key,保证「周期起点 → 标签」和「交易 → 标签」同一口径。
    labels = [_bucket_key("year", start, tz_offset_minutes, month_start_day) for start in starts]
    current_label = labels[-1]
    completed_labels = labels[:-1]

    restricted_to_creator = (
        count_ledger_members(
            db,
            # ledger_access.count_ledger_members 把 ledger_id 标成 int,实际
            # Ledger.id 是 String(36)。修签名要动 src/ledger_access.py(本次
            # 任务范围外),同 read/ledgers.py:50 的既有调用。
            ledger_id=ledger.id,  # type: ignore[arg-type]
        )
        > 1
    )

    rows = db.execute(
        select(
            ReadTxProjection.tx_type,
            # 账本维度折本位币口径(0018):native_amount ?? amount。
            # 账户维度才读原币 amount,别仿此改(_shared.py:436-437)。
            func.coalesce(ReadTxProjection.native_amount, ReadTxProjection.amount),
            ReadTxProjection.happened_at,
            ReadTxProjection.category_name,
            ReadTxProjection.created_by_user_id,
            ReadTxProjection.exclude_from_stats,
        ).where(
            ReadTxProjection.ledger_id == ledger.id,
            ReadTxProjection.tx_type.in_(("income", "expense")),  # 跳过 transfer
            ReadTxProjection.happened_at >= window_start,
            ReadTxProjection.happened_at < window_end,
        )
    ).all()

    # 窗口内没有交易的周期补 0(空周期是真的没花钱),否则 median / MAD 只反映
    # 「有记账的月」,基线系统性偏高。
    totals: dict[str, dict[str, float]] = {
        label: {"income": 0.0, "expense": 0.0} for label in labels
    }
    category_by_period: dict[str, dict[str, float]] = {label: {} for label in labels}

    for tx_type, amount, happened_at, category_name, creator, excluded in rows:
        if happened_at is None:
            continue
        if restricted_to_creator and creator != user_id:
            continue
        if excluded:  # exclude_from_stats:不进收支/结余/分类统计(设计 D1)
            continue
        label = _bucket_key("year", happened_at, tz_offset_minutes, month_start_day)
        slot = totals.get(label)
        if slot is None:
            # tz_offset 非 0 时,贴着周期边界的几个小时可能折出窗口外的标签。
            # 上限:这类行不计入。tz_offset=0(默认)不会走到这里。
            continue
        value = float(amount or 0.0)
        if tx_type == "income":
            slot["income"] += value
            continue
        slot["expense"] += value
        name = (category_name or "").strip() or "Uncategorized"
        bucket_categories = category_by_period[label]
        bucket_categories[name] = bucket_categories.get(name, 0.0) + value

    return InsightWindow(
        periods=tuple(
            PeriodTotals(
                bucket=label,
                income=totals[label]["income"],
                expense=totals[label]["expense"],
            )
            for label in completed_labels
        ),
        category_by_period=category_by_period,
        start_at=window_start,
        end_at=window_end,
        restricted_to_creator=restricted_to_creator,
        current=PeriodTotals(
            bucket=current_label,
            income=totals[current_label]["income"],
            expense=totals[current_label]["expense"],
        ),
    )


__all__ = ["budget_usage_rows", "load_insight_window", "period_progress"]
