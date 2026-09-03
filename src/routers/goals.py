"""攒钱目标(financial goals)CRUD endpoint。

4 个 endpoint:
  - GET    /ledgers/{ext}/goals?status=active|achieved|archived|all
  - POST   /ledgers/{ext}/goals
  - PATCH  /ledgers/{ext}/goals/{goal_id}
  - DELETE /ledgers/{ext}/goals/{goal_id}     → 204

**server-only 实体,不参与同步**:目标只存服务端(参照
personal_access_tokens / backup_remotes 的先例),没有 read_*_projection、
不写 sync_changes、不登记 sync_applier 的三张 dispatch 表、没有 /write/*
端点。mobile(Flutter)当前看不到目标数据。

**两层鉴权,第二层不可省**:

1. ``require_accessible_ledger_by_external_id`` —— caller 必须是该账本成员
   (写操作再限 ``WRITABLE_ROLES``)。不通过一律 404,不返 403,不泄露账本
   存在性。
2. 每条查询再叠 ``FinancialGoal.user_id == current_user.id`` —— 共享账本里
   成员 B 既看不到也改不了成员 A 的目标。别人的目标一律 404(不是 200 空
   列表、也不是 403)。

金额派生量(``progress_pct`` / ``remaining_amount``)在服务端算完给前端,
避免每个客户端各算一遍算出不同结果。服务端只回数字和 enum code,不回文案。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_serializer, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_any_scopes
from ..ledger_access import (
    WRITABLE_ROLES,
    require_accessible_ledger_by_external_id,
)
from ..models import FinancialGoal, User
from ..security import SCOPE_APP_WRITE, SCOPE_WEB_READ, SCOPE_WEB_WRITE
from ..services.goal_plan import (
    GoalDemand,
    allocate,
    classify_feasibility,
    months_remaining,
    projected_months,
    required_monthly,
)
from ..services.insight_loader import load_insight_window
from ..services.insights import compute_surplus_baseline

router = APIRouter()
logger = logging.getLogger(__name__)

_READ_SCOPE_DEP = require_any_scopes(SCOPE_APP_WRITE, SCOPE_WEB_READ)
_WRITE_SCOPE_DEP = require_any_scopes(SCOPE_APP_WRITE, SCOPE_WEB_WRITE)

# 权重上界。纯防御性:没有场景需要 100 以上的相对权重,放开只会让分配算法
# 的数值范围失控。
_MAX_PRIORITY = 100

GoalStatus = Literal["active", "achieved", "archived"]
GoalStatusFilter = Literal["active", "achieved", "archived", "all"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    """SQLite 不保留 tzinfo,序列化时强制补 UTC 标记,前端才能转对本地时间。"""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


def _validate_future_deadline(value: date | None) -> date | None:
    """deadline 必须严格晚于「UTC 今天」。

    POST 和 PATCH 用同一条规则:过去的截止日期在两条路径上都拒(422)。
    已经过期的目标该走 ``status=archived``,不该靠改 deadline 表达。
    """
    if value is not None and value <= _utcnow().date():
        raise ValueError("deadline must be a future date")
    return value


def _normalize_name(value: str) -> str:
    """去空白后不能为空 —— ``min_length=1`` 拦不住 ``"   "``。"""
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    return stripped


class GoalCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    target_amount: float = Field(gt=0)
    # 允许 saved_amount > target_amount(超额攒完很正常),只拒负数
    saved_amount: float = Field(default=0.0, ge=0)
    deadline: date | None = None
    priority: int = Field(default=0, ge=0, le=_MAX_PRIORITY)
    status: GoalStatus = "active"

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _normalize_name(v)

    @field_validator("deadline")
    @classmethod
    def _check_deadline(cls, v: date | None) -> date | None:
        return _validate_future_deadline(v)


class GoalPatchRequest(BaseModel):
    """部分更新:只有显式给出的字段会改,``None`` = 不动这个字段。

    代价是 ``deadline`` 一旦设上就没法清空(``None`` 被当成「不动」)。
    需要清空时删掉目标重建 —— 目标本身是轻量实体,不值得为此加
    sentinel 值把语义搞复杂。
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    target_amount: float | None = Field(default=None, gt=0)
    saved_amount: float | None = Field(default=None, ge=0)
    deadline: date | None = None
    priority: int | None = Field(default=None, ge=0, le=_MAX_PRIORITY)
    status: GoalStatus | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None) -> str | None:
        return _normalize_name(v) if v is not None else None

    @field_validator("deadline")
    @classmethod
    def _check_deadline(cls, v: date | None) -> date | None:
        return _validate_future_deadline(v)


class GoalItem(BaseModel):
    id: str
    ledger_id: str  # 账本 external_id(客户端口径),不是内部主键
    name: str
    target_amount: float
    saved_amount: float
    # 服务端算好的派生量,前端直接用
    remaining_amount: float
    progress_pct: float
    currency: str
    deadline: date | None
    priority: int
    status: GoalStatus
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _ser_dt(self, v: datetime) -> str:
        return _utc_iso(v)


class GoalListResponse(BaseModel):
    ledger_id: str
    ledger_currency: str
    items: list[GoalItem] = Field(default_factory=list)


def _to_item(goal: FinancialGoal, ledger_external_id: str) -> GoalItem:
    target = goal.target_amount or 0.0
    saved = goal.saved_amount or 0.0
    # target 走 API 校验必 > 0,这里仍兜一层 —— 手工改库 / 历史脏数据不该 500
    progress = round(saved / target * 100.0, 1) if target > 0 else 0.0
    return GoalItem(
        id=goal.id,
        ledger_id=ledger_external_id,
        name=goal.name,
        target_amount=target,
        saved_amount=saved,
        remaining_amount=max(0.0, target - saved),
        progress_pct=progress,
        currency=goal.currency,
        deadline=goal.deadline,
        priority=goal.priority,
        # DB 列是 String(16),只有本 router 写它;Literal 收窄靠 API 层校验
        status=goal.status,  # type: ignore[arg-type]
        created_at=goal.created_at,
        updated_at=goal.updated_at,
    )


def _load_goal(db: Session, *, goal_id: str, ledger_id: str, user_id: str) -> FinancialGoal:
    """第二层鉴权:目标必须同时属于该账本 **且** 属于 caller,否则 404。"""
    goal = db.scalar(
        select(FinancialGoal).where(
            FinancialGoal.id == goal_id,
            FinancialGoal.ledger_id == ledger_id,
            FinancialGoal.user_id == user_id,
        )
    )
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return goal


@router.get(
    "/ledgers/{ledger_external_id}/goals",
    response_model=GoalListResponse,
)
def list_goals(
    ledger_external_id: str,
    # 默认 all:读端点不替前端做隐式过滤,要哪种状态自己说
    status_filter: GoalStatusFilter = Query(default="all", alias="status"),
    _scopes: set[str] = Depends(_READ_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GoalListResponse:
    ledger, _role = require_accessible_ledger_by_external_id(
        db,
        user_id=current_user.id,
        ledger_external_id=ledger_external_id,
    )

    q = select(FinancialGoal).where(
        FinancialGoal.ledger_id == ledger.id,
        FinancialGoal.user_id == current_user.id,  # 第二层:只看自己的
    )
    if status_filter != "all":
        q = q.where(FinancialGoal.status == status_filter)
    # priority 降序为主序(分配权重高的排前),created_at / id 兜确定性顺序。
    # deadline 不进排序键:NULLS LAST 在 SQLite / Postgres 上行为不一致,
    # 排 deadline 交给前端。
    q = q.order_by(
        FinancialGoal.priority.desc(),
        FinancialGoal.created_at.asc(),
        FinancialGoal.id.asc(),
    )
    goals = list(db.scalars(q).all())

    logger.info(
        "goals.list ledger=%s status=%s rows=%d user=%s",
        ledger_external_id,
        status_filter,
        len(goals),
        current_user.id,
    )
    return GoalListResponse(
        ledger_id=ledger_external_id,
        ledger_currency=ledger.currency or "CNY",
        items=[_to_item(g, ledger_external_id) for g in goals],
    )


@router.post(
    "/ledgers/{ledger_external_id}/goals",
    response_model=GoalItem,
    status_code=status.HTTP_201_CREATED,
)
def create_goal(
    ledger_external_id: str,
    req: GoalCreateRequest,
    _scopes: set[str] = Depends(_WRITE_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GoalItem:
    ledger, _role = require_accessible_ledger_by_external_id(
        db,
        user_id=current_user.id,
        ledger_external_id=ledger_external_id,
        roles=WRITABLE_ROLES,
    )

    goal = FinancialGoal(
        user_id=current_user.id,
        ledger_id=ledger.id,
        name=req.name,
        target_amount=req.target_amount,
        saved_amount=req.saved_amount,
        # 币种快照:此刻账本的币种。账本日后改币种不回写(见 model docstring)
        currency=ledger.currency or "CNY",
        deadline=req.deadline,
        priority=req.priority,
        status=req.status,
    )
    db.add(goal)
    db.commit()

    logger.info(
        "goals.create ledger=%s goal=%s user=%s target=%s currency=%s status=%s",
        ledger_external_id,
        goal.id,
        current_user.id,
        goal.target_amount,
        goal.currency,
        goal.status,
    )
    return _to_item(goal, ledger_external_id)


@router.patch(
    "/ledgers/{ledger_external_id}/goals/{goal_id}",
    response_model=GoalItem,
)
def patch_goal(
    ledger_external_id: str,
    goal_id: str,
    req: GoalPatchRequest,
    _scopes: set[str] = Depends(_WRITE_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GoalItem:
    ledger, _role = require_accessible_ledger_by_external_id(
        db,
        user_id=current_user.id,
        ledger_external_id=ledger_external_id,
        roles=WRITABLE_ROLES,
    )
    goal = _load_goal(db, goal_id=goal_id, ledger_id=ledger.id, user_id=current_user.id)

    # 只更新显式提供的字段,None 表示不改(同 profile PATCH 口径)
    if req.name is not None:
        goal.name = req.name
    if req.target_amount is not None:
        goal.target_amount = req.target_amount
    if req.saved_amount is not None:
        goal.saved_amount = req.saved_amount
    if req.deadline is not None:
        goal.deadline = req.deadline
    if req.priority is not None:
        goal.priority = req.priority
    if req.status is not None:
        goal.status = req.status
    # 显式 bump:空 body 的 PATCH 不会触发 onupdate,但也算一次"确认过"
    goal.updated_at = _utcnow()
    db.commit()

    logger.info(
        "goals.patch ledger=%s goal=%s user=%s saved=%s status=%s",
        ledger_external_id,
        goal.id,
        current_user.id,
        goal.saved_amount,
        goal.status,
    )
    return _to_item(goal, ledger_external_id)


@router.delete(
    "/ledgers/{ledger_external_id}/goals/{goal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_goal(
    ledger_external_id: str,
    goal_id: str,
    _scopes: set[str] = Depends(_WRITE_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    ledger, _role = require_accessible_ledger_by_external_id(
        db,
        user_id=current_user.id,
        ledger_external_id=ledger_external_id,
        roles=WRITABLE_ROLES,
    )
    goal = _load_goal(db, goal_id=goal_id, ledger_id=ledger.id, user_id=current_user.id)

    db.delete(goal)
    db.commit()
    logger.info(
        "goals.delete ledger=%s goal=%s user=%s",
        ledger_external_id,
        goal_id,
        current_user.id,
    )


# ---------------------------------------------------------------------------
# 目标可行性 + 结余分配(GET /ledgers/{ext}/goals/plan)
# ---------------------------------------------------------------------------

# 窗口上下界跟 `routers/insights.py::get_ledger_insights` 对齐 —— 两个端点吃同一
# 个 `load_insight_window` + `compute_surplus_baseline`,参数范围不一致会让前端
# 拿到两个对不上的 allocatable。下界 3:1-2 个周期的 median 没有统计意义;
# 上界 24:两年前的消费习惯已经不代表现在,而且行扫成本线性上涨。
_MIN_LOOKBACK_PERIODS = 3
_MAX_LOOKBACK_PERIODS = 24

# 金额在响应边界统一取 2 位小数,前端不该渲染 0.009999999999990905 这种浮点
# 噪声。判定阈值本身在 `services/goal_plan.py`,这里只管展示口径。
_MONEY_DECIMALS = 2


def _money(value: float) -> float:
    return round(value, _MONEY_DECIMALS)


class GoalPlanBaseline(BaseModel):
    """结余基线,原样透出 `services/insights.py::compute_surplus_baseline` 的结果。

    `buffer` 是用户自己月支出的 MAD(不是固定百分比),`allocatable` 已经是
    `max(0, median_surplus - buffer)`,前端不用再算一遍。
    """

    median_income: float
    median_expense: float
    median_surplus: float
    buffer: float
    allocatable: float
    basis_periods: int


class GoalPlanItem(BaseModel):
    """`feasibility` ∈ {feasible, tight, infeasible, no_deadline},判定规则见
    `services/goal_plan.py::classify_feasibility`。服务端只回数字和 code,
    「延后 deadline / 提高月供 / 降低目标」这三个选项由前端组装。"""

    goal_id: str
    name: str
    target_amount: float
    saved_amount: float
    remaining_amount: float
    priority: int
    deadline: date | None
    # 无 deadline 时三者皆 None:没有期限就没有「每月必须存多少」
    months_remaining: int | None
    required_monthly: float | None
    allocated_monthly: float
    # max(0, required - allocated),无 deadline 记 0
    shortfall: float
    projected_months: int | None
    feasibility: str


class GoalPlanResponse(BaseModel):
    """`items` 只含 `status == "active"` 的目标 —— achieved / archived 不参与分配,
    列出来只会让「总共需要多少」这个数失真。"""

    ledger_id: str
    currency: str
    restricted_to_creator: bool
    baseline: GoalPlanBaseline
    allocatable: float
    total_required_monthly: float
    unallocated: float
    items: list[GoalPlanItem] = Field(default_factory=list)


@router.get(
    "/ledgers/{ledger_external_id}/goals/plan",
    response_model=GoalPlanResponse,
)
def get_goals_plan(
    ledger_external_id: str,
    lookback_periods: int = Query(default=6, ge=_MIN_LOOKBACK_PERIODS, le=_MAX_LOOKBACK_PERIODS),
    tz_offset_minutes: int = Query(default=0, ge=-720, le=840),
    _scopes: set[str] = Depends(_READ_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GoalPlanResponse:
    ledger, _role = require_accessible_ledger_by_external_id(
        db,
        user_id=current_user.id,
        ledger_external_id=ledger_external_id,
    )

    # 结余基线复用洞察那条链,不在这里重新推导「每月能存多少」:仓里只能有一套
    # allocatable 口径(见 services/insights.py 模块 docstring)。
    window = load_insight_window(
        db,
        ledger=ledger,
        user_id=current_user.id,
        lookback_periods=lookback_periods,
        tz_offset_minutes=tz_offset_minutes,
    )
    baseline = compute_surplus_baseline(window.periods)

    q = (
        select(FinancialGoal)
        .where(
            FinancialGoal.ledger_id == ledger.id,
            FinancialGoal.user_id == current_user.id,  # 第二层:只看自己的
            FinancialGoal.status == "active",
        )
        # 同 list_goals 的排序键:priority 降序为主,created_at / id 兜确定性
        .order_by(
            FinancialGoal.priority.desc(),
            FinancialGoal.created_at.asc(),
            FinancialGoal.id.asc(),
        )
    )
    goals = list(db.scalars(q).all())

    # ponytail: 目标 currency 是创建时的账本币种快照,这里按同一币种直接相加。
    # 上限是账本改过币种后老目标混币累加。升级路径:等目标接上汇率表时按
    # `ledger.currency` 折算再分配。
    demands = [
        GoalDemand(
            goal_id=goal.id,
            remaining_amount=max(0.0, (goal.target_amount or 0.0) - (goal.saved_amount or 0.0)),
            priority=goal.priority or 0,
        )
        for goal in goals
    ]
    allocation = allocate(demands, baseline.allocatable)

    today = _utcnow().date()
    items: list[GoalPlanItem] = []
    total_required = 0.0
    for goal, demand in zip(goals, demands, strict=True):
        months = months_remaining(goal.deadline, today)
        required = required_monthly(demand.remaining_amount, months)
        allocated = allocation.by_goal[demand.goal_id]
        if required is not None:
            total_required += required
        items.append(
            GoalPlanItem(
                goal_id=goal.id,
                name=goal.name,
                target_amount=_money(goal.target_amount or 0.0),
                saved_amount=_money(goal.saved_amount or 0.0),
                remaining_amount=_money(demand.remaining_amount),
                priority=demand.priority,
                deadline=goal.deadline,
                months_remaining=months,
                required_monthly=None if required is None else _money(required),
                allocated_monthly=_money(allocated),
                shortfall=(0.0 if required is None else _money(max(0.0, required - allocated))),
                projected_months=projected_months(demand.remaining_amount, allocated),
                feasibility=classify_feasibility(demand.remaining_amount, required, allocated),
            )
        )

    logger.info(
        "goals.plan ledger=%s goals=%d allocatable=%.2f restricted=%s user=%s",
        ledger_external_id,
        len(items),
        baseline.allocatable,
        window.restricted_to_creator,
        current_user.id,
    )
    return GoalPlanResponse(
        ledger_id=ledger_external_id,
        currency=ledger.currency or "CNY",
        restricted_to_creator=window.restricted_to_creator,
        baseline=GoalPlanBaseline(
            median_income=_money(baseline.median_income),
            median_expense=_money(baseline.median_expense),
            median_surplus=_money(baseline.median_surplus),
            buffer=_money(baseline.buffer),
            allocatable=_money(baseline.allocatable),
            basis_periods=baseline.basis_periods,
        ),
        allocatable=_money(baseline.allocatable),
        total_required_monthly=_money(total_required),
        unallocated=_money(allocation.unallocated),
        items=items,
    )
