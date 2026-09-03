"""账本财务洞察 endpoint。

GET /api/v1/ledgers/{ledger_external_id}/insights
  ?lookback_periods=6   (3-24)  &tz_offset_minutes=0

把已有的交易 / 预算 projection 换算成能直接照着做决定的数字:跨周期基线、
按用户自己习惯算出来的预算建议、剩余天数摊平的日均可花额度、一个诊断 code。

**纯读端点,不是同步实体**:不写 `ledger_snapshot`、不插 `sync_changes`、
没有 `read_*_projection` 表,所以 CLAUDE.md 里「新增 entity」那六步不适用。

三层职责(见 `services/insights.py` 模块 docstring):统计口径在
`services/insights.py`,DB 装载在 `services/insight_loader.py`,本文件只做
HTTP 组装 + 鉴权。

**只回数字和 enum code,不回文案**:本仓 locale 只到 zh / zh-CN / zh-TW / en
(`routers/ai/ask.py:56`),没有别的语种可落地,措辞一律交前端按自己的 locale
决定。`diagnosis` 的 4 个取值见 `LedgerInsightsResponse.diagnosis`。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_any_scopes
from ..ledger_access import require_accessible_ledger_by_external_id
from ..models import User, UserCategoryProjection
from ..security import SCOPE_APP_WRITE, SCOPE_WEB_READ
from ..services.insight_loader import (
    budget_usage_rows,
    load_insight_window,
    period_progress,
)
from ..services.insights import (
    classify_diagnosis,
    compute_category_baselines,
    compute_safe_daily,
    compute_surplus_baseline,
)
from .read.ledgers import _current_period_range

router = APIRouter()
logger = logging.getLogger(__name__)

_READ_SCOPE_DEP = require_any_scopes(SCOPE_APP_WRITE, SCOPE_WEB_READ)

# 窗口下界 3:1-2 个周期的 median 没有统计意义(见 services/insights.py 的
# _DIAGNOSIS_MIN_PERIODS)。上界 24:再长的窗口里两年前的消费习惯已经不代表
# 现在,而且行扫成本线性上涨。
_MIN_LOOKBACK_PERIODS = 3
_MAX_LOOKBACK_PERIODS = 24


def _utc_iso(value: datetime) -> str:
    """SQLite 不保留 tzinfo,序列化时强制补 UTC 标记,前端才能转对本地时间
    (同 `routers/goals.py` / `routers/invites.py`)。"""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


class InsightRangeOut(BaseModel):
    """窗口范围。

    `restricted_to_creator=True` 表示所有数字只统计了 caller 自己记的账(多成员
    共享账本),跟账本「全员合计」不是一个量 —— 必须透出,否则前端会把它当成
    整个账本的数字展示。
    """

    start_at: datetime
    end_at: datetime
    basis_periods: int
    restricted_to_creator: bool

    @field_serializer("start_at", "end_at")
    def _ser_dt(self, v: datetime) -> str:
        return _utc_iso(v)


class InsightCurrentOut(BaseModel):
    bucket: str
    income: float
    expense: float
    surplus: float
    days_total: int
    days_remaining: int
    days_elapsed: int


class InsightBaselineOut(BaseModel):
    median_income: float
    median_expense: float
    median_surplus: float
    buffer: float
    allocatable: float


class InsightSeriesItemOut(BaseModel):
    bucket: str
    income: float
    expense: float
    surplus: float


class InsightCategoryBaselineOut(BaseModel):
    category_name: str
    median: float
    mean: float
    periods_present: int


class InsightBudgetStatusOut(BaseModel):
    budget_id: str
    budget_type: str
    category_id: str | None
    category_name: str | None
    amount: float
    used: float
    remaining: float
    percent_used: float
    safe_daily: float
    exceeded: bool


class InsightRecommendationCategoryOut(BaseModel):
    category_name: str
    recommended_amount: float
    basis_periods: int


class InsightRecommendationOut(BaseModel):
    categories: list[InsightRecommendationCategoryOut] = Field(default_factory=list)
    saving_amount: float
    buffer_amount: float


class LedgerInsightsResponse(BaseModel):
    """`diagnosis` ∈ {insufficient_data, reduce_spending, increase_income, on_track},
    判定规则见 `services/insights.py::classify_diagnosis`。"""

    ledger_id: str
    currency: str
    month_start_day: int
    range: InsightRangeOut
    current: InsightCurrentOut
    baseline: InsightBaselineOut
    series: list[InsightSeriesItemOut] = Field(default_factory=list)
    category_baselines: list[InsightCategoryBaselineOut] = Field(default_factory=list)
    budget_status: list[InsightBudgetStatusOut] = Field(default_factory=list)
    recommendation: InsightRecommendationOut
    diagnosis: str


def _category_name_by_sync_id(db: Session, *, user_id: str) -> dict[str, str]:
    """category sync_id → name。跟 `read/ledgers.py::list_budgets` 同一份映射
    (user-global 维度,同 sync_id 重复时第一条胜出)。"""
    rows = db.execute(
        select(UserCategoryProjection.sync_id, UserCategoryProjection.name)
        .where(UserCategoryProjection.user_id == user_id)
        .order_by(UserCategoryProjection.sync_id.asc())
    ).all()
    out: dict[str, str] = {}
    for row in rows:
        if row.sync_id not in out:
            out[row.sync_id] = (row.name or "").strip()
    return out


@router.get(
    "/ledgers/{ledger_external_id}/insights",
    response_model=LedgerInsightsResponse,
)
def get_ledger_insights(
    ledger_external_id: str,
    lookback_periods: int = Query(default=6, ge=_MIN_LOOKBACK_PERIODS, le=_MAX_LOOKBACK_PERIODS),
    tz_offset_minutes: int = Query(default=0, ge=-720, le=840),
    _scopes: set[str] = Depends(_READ_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LedgerInsightsResponse:
    ledger, _role = require_accessible_ledger_by_external_id(
        db,
        user_id=current_user.id,
        ledger_external_id=ledger_external_id,
    )

    window = load_insight_window(
        db,
        ledger=ledger,
        user_id=current_user.id,
        lookback_periods=lookback_periods,
        tz_offset_minutes=tz_offset_minutes,
    )
    baseline = compute_surplus_baseline(window.periods)
    # 分类基线只看已完成周期 —— 当前半个周期的分类支出会把 median 拽低。
    completed_buckets = [p.bucket for p in window.periods]
    category_baselines = compute_category_baselines(window.category_by_period, completed_buckets)

    # 预算用量和「还剩几天」必须落在**当前**周期上(窗口跨多个周期)。这里跟
    # loader 各算一次 _current_period_range —— 同一个函数同一个入参,结果一致。
    now = datetime.now(timezone.utc)
    current_start, current_end = _current_period_range(ledger.month_start_day or 1, now)
    days_total, days_remaining = period_progress(current_start, current_end, now)

    name_by_sync_id = _category_name_by_sync_id(db, user_id=current_user.id)
    budget_status: list[InsightBudgetStatusOut] = []
    for budget, used in budget_usage_rows(db, ledger=ledger, start=current_start, end=current_end):
        amount = float(budget.amount or 0.0)
        remaining = max(0.0, amount - used)
        budget_status.append(
            InsightBudgetStatusOut(
                budget_id=budget.sync_id,
                budget_type=budget.budget_type or "total",
                category_id=budget.category_sync_id,
                category_name=(
                    name_by_sync_id.get(budget.category_sync_id)
                    if budget.category_sync_id
                    else None
                ),
                amount=amount,
                used=used,
                remaining=remaining,
                # 跟 mcp/tools/read_tools.py:315 同口径:1 位小数
                percent_used=round(used / amount * 100, 1) if amount else 0.0,
                safe_daily=compute_safe_daily(remaining, days_remaining),
                exceeded=used > amount,
            )
        )
    # dedup 之后的顺序来自 DB 行序,排一次保证同样输入同样顺序。
    budget_status.sort(key=lambda b: (b.budget_type, b.category_name or "", b.budget_id))

    recommendation = InsightRecommendationOut(
        # recommended_amount 就是该分类的跨周期 median,**不乘任何系数** ——
        # 凭空加的 1.1 倍之类只是把「按你自己的习惯」变成又一个人挑的模板。
        # median 为 0 的分类不给建议(窗口里只出现过一两次的偶发分类),
        # 回一条 0 元预算建议没有意义。
        categories=[
            InsightRecommendationCategoryOut(
                category_name=item.category_name,
                recommended_amount=item.median,
                basis_periods=baseline.basis_periods,
            )
            for item in category_baselines
            if item.median > 0
        ],
        saving_amount=baseline.allocatable,
        buffer_amount=baseline.buffer,
    )
    diagnosis = classify_diagnosis(baseline, window.current)

    logger.info(
        "insights.get ledger=%s periods=%d restricted=%s diagnosis=%s caller=%s",
        ledger_external_id,
        baseline.basis_periods,
        window.restricted_to_creator,
        diagnosis,
        current_user.id,
    )

    return LedgerInsightsResponse(
        ledger_id=ledger_external_id,
        currency=ledger.currency or "CNY",
        month_start_day=ledger.month_start_day or 1,
        range=InsightRangeOut(
            start_at=window.start_at,
            end_at=window.end_at,
            basis_periods=baseline.basis_periods,
            restricted_to_creator=window.restricted_to_creator,
        ),
        current=InsightCurrentOut(
            bucket=window.current.bucket,
            income=window.current.income,
            expense=window.current.expense,
            surplus=window.current.surplus,
            days_total=days_total,
            days_remaining=days_remaining,
            days_elapsed=max(0, days_total - days_remaining),
        ),
        baseline=InsightBaselineOut(
            median_income=baseline.median_income,
            median_expense=baseline.median_expense,
            median_surplus=baseline.median_surplus,
            buffer=baseline.buffer,
            allocatable=baseline.allocatable,
        ),
        series=[
            InsightSeriesItemOut(
                bucket=p.bucket,
                income=p.income,
                expense=p.expense,
                surplus=p.surplus,
            )
            for p in window.periods
        ],
        category_baselines=[
            InsightCategoryBaselineOut(
                category_name=item.category_name,
                median=item.median,
                mean=item.mean,
                periods_present=item.periods_present,
            )
            for item in category_baselines
        ],
        budget_status=budget_status,
        recommendation=recommendation,
        diagnosis=diagnosis,
    )
