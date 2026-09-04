"""POST /api/v1/ai/income-growth — 收入增长建议(个人财务智能 #11)。

两半,职责完全分开:

- **确定性那半**(`assessment`):结余偏薄到底是支出问题还是收入问题、每月还差
  多少钱。全部来自既有服务层(`services/insight_loader.py` 装载 →
  `services/insights.py` 算基线 → `services/goal_plan.py` 算目标缺口 →
  `services/income_growth.py` 判 lever / gap),**不经过 LLM**。
- **生成那半**(`suggestions`):把 `assessment` + 用户自己填的职业档案喂给用户
  自己配的 chat provider,要具体的挣钱办法。没配 provider / 数据不足 / 没填档案
  时整段为空。

## 为什么降级是 200 而不是 400

`routers/ai/ask.py:78` 没绑 provider 时返 400 `AI_NO_CHAT_PROVIDER`,因为它除了
LLM 输出之外**没有任何东西可返**。本端点不一样:`assessment` 才是主要价值,LLM
建议是加分项。所以所有降级路径一律 **HTTP 200 + `generated=false` +
`generated_reason` code**,`assessment` 照常完整返回。

**不要把这里"修"回 400。** 那会让一个没配 AI 的用户连自己的结余诊断都看不到。

鉴权失败仍然照常抛:scope 不足 403(`require_any_scopes`),账本不可访问 404
(`require_accessible_ledger_by_external_id`,不返 403 以免泄露账本存在性)。

## 不流式

`ask.py` 走 SSE,代价是 header 一发出去就再也没法返回真实 HTTP 状态码(它的
provider 错误只能塞进 SSE 帧里)。本端点返回单个结构化 JSON body,错误码因此
是诚实的,响应也可测、可缓存。所以用 `call_chat_json`,不用
`stream_chat_completion`。

**纯读端点,不是同步实体**:不写 `ledger_snapshot`、不插 `sync_changes`、没有
`read_*_projection` 表,所以 CLAUDE.md 里「新增 entity」那六步不适用。
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...database import get_db
from ...deps import get_current_user, require_any_scopes
from ...ledger_access import require_accessible_ledger_by_external_id
from ...models import FinancialGoal, User, UserProfile
from ...schemas import CareerProfile
from ...security import SCOPE_APP_WRITE, SCOPE_WEB_READ, SCOPE_WEB_WRITE
from ...services.ai import (
    ChatProviderError,
    NoChatProviderError,
    call_chat_json,
    resolve_chat_provider,
)
from ...services.ai.prompts import build_income_growth_messages
from ...services.goal_plan import GoalDemand, allocate, months_remaining, required_monthly
from ...services.income_growth import (
    MAX_SUGGESTIONS,
    classify_gap,
    classify_lever,
    surplus_ratio,
)
from ...services.insight_loader import load_insight_window
from ...services.insights import classify_diagnosis, compute_surplus_baseline

logger = logging.getLogger(__name__)
router = APIRouter()

# 读形态的分析端点,不写任何东西 → 跟 `ask.py:46` 同一套 scope,而不是
# `parse_tx_text.py:38` 那套写 scope。
_INCOME_GROWTH_SCOPE_DEP = require_any_scopes(SCOPE_APP_WRITE, SCOPE_WEB_READ, SCOPE_WEB_WRITE)

# 窗口上下界跟 `routers/insights.py` / `routers/goals.py::get_goals_plan` 对齐 ——
# 三个端点吃同一个 `load_insight_window` + `compute_surplus_baseline`,参数范围不
# 一致会让前端拿到互相对不上的 median。
_MIN_LOOKBACK_PERIODS = 3
_MAX_LOOKBACK_PERIODS = 24

# 金额在响应边界统一 2 位小数,前端不该渲染 0.009999999999990905 这种浮点噪声
# (同 `routers/goals.py::_money`)。
_MONEY_DECIMALS = 2
# 结余率保留 4 位 = 万分位,足够渲染成带两位小数的百分比(0.0413 → 4.13%),
# 又不把 0.039999999999999994 这种噪声透给前端。
_RATIO_DECIMALS = 4

# LLM 输出的长度上界。超出直接截断,不报错:模型不听 prompt 里的字数要求是常态,
# 为此 422 掉整个响应等于让确定性那半也一起消失。
_MAX_TITLE_CHARS = 120
_MAX_RATIONALE_CHARS = 400

# 输出 token 上界。8xx 足够放 5 条建议(每条一句 title + 一段 rationale + 4 个
# 数字);再高只是给跑偏的模型更多空间。注意有些自建网关会**静默丢弃**不认识的
# 参数,所以这不是唯一的成本上界 —— 限流和 timeout 才是(见下)。
_LLM_MAX_TOKENS = 800
# 20 秒 + 不重试。默认的 `timeout=30, max_retries=1` 在最坏情况下是
# 2 轮 × (1 + _MAX_PARAM_STRIPS=3) = 8 次 POST × 30s ≈ 240s 的上游时间,而这是
# 一个用户点了按钮在等的交互请求 —— 一份建议清单不值那个等待。`max_retries=0`
# 之后单轮内的自适应摘参数(`_post_chat_adaptive`)仍然生效,兼容性不受影响。
_LLM_TIMEOUT_S = 20.0
_LLM_MAX_RETRIES = 0

# ──────────────── 速率限制(内存) ────────────────
#
# 抄 `routers/ai/test_provider.py:74-83` 的滑动窗口。10 次 / 300 秒:这是一个
# 「点一下按钮」的功能,不是每次击键都触发的功能,而每次触发都可能是一次付费
# API 调用。
#
# **上限:窗口是每 worker 进程一份,不是全局的**(同既有限流器)。多 worker 部署
# 下实际配额是 10 × worker 数。升级路径是挪到 Redis / 数据库计数,但那要引入
# 新依赖,当前收益撑不起。
_RATE_WINDOWS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LIMIT_WINDOW_S = 300.0
_RATE_LIMIT_MAX = 10


def _check_rate_limit(user_id: str) -> bool:
    """单 user `_RATE_LIMIT_WINDOW_S` 秒内最多 `_RATE_LIMIT_MAX` 次。True = 放行。"""
    now = time.monotonic()
    window = _RATE_WINDOWS[user_id]
    while window and now - window[0] > _RATE_LIMIT_WINDOW_S:
        window.popleft()
    if len(window) >= _RATE_LIMIT_MAX:
        return False
    window.append(now)
    return True


# ──────────────── 请求 / 响应 schema(本文件私有) ────────────────
#
# pydantic 模型一律留在 router 文件里,不进 `src/schemas.py` —— 同 `ask.py` /
# `parse_tx_text.py`,`src/schemas.py` 只放跨端点共享的形状。

SuggestionKind = Literal[
    "freelance", "side_project", "skill_upgrade", "job_switch", "passive", "other"
]

_SUGGESTION_KINDS: frozenset[str] = frozenset(
    ("freelance", "side_project", "skill_upgrade", "job_switch", "passive", "other")
)

GeneratedReason = Literal[
    "no_provider",
    "no_career_profile",
    "insufficient_data",
    "lever_not_income",
    "provider_failed",
]


class IncomeGrowthRequest(BaseModel):
    """`ledger_id` 是 `Ledger.external_id`(客户端口径),不是内部主键。

    locale 的 pattern 跟 `ask.py:56` / `parse_tx_text.py:46` 逐字相同 —— 本仓只有
    zh / zh-CN / zh-TW / en 四种语言可落地,别的语种没有对应的 prompt 分支。
    """

    ledger_id: str = Field(min_length=1, max_length=255)
    locale: str = Field(default="zh", pattern=r"^(zh|zh-CN|zh-TW|en)$")
    lookback_periods: int = Field(default=6, ge=_MIN_LOOKBACK_PERIODS, le=_MAX_LOOKBACK_PERIODS)
    tz_offset_minutes: int = Field(default=0, ge=-720, le=840)


class IncomeGrowthAssessment(BaseModel):
    """确定性评估,**任何情况下都完整返回**(包括所有降级路径)。

    - `diagnosis` ∈ {insufficient_data, on_track, reduce_spending, increase_income},
      判定规则见 `services/insights.py::classify_diagnosis`。
    - `lever` ∈ {income, spending, both, none},见
      `services/income_growth.py::classify_lever`。
    - `gap_basis` ∈ {goal_shortfall, buffer_deficit, surplus_floor, none},优先级
      见 `services/income_growth.py::classify_gap`。
    - `surplus_ratio` 是比例不是百分比:1.0 = 100%。收入 <= 0 时为 0.0。
    """

    diagnosis: str
    lever: str
    median_income: float
    median_expense: float
    median_surplus: float
    buffer: float
    surplus_ratio: float
    basis_periods: int
    target_monthly_gap: float
    gap_basis: str


class IncomeGrowthSuggestion(BaseModel):
    """LLM 生成的一条建议。数字类字段允许 None —— prompt 明确要求「估不出来填
    null」,一个编出来的具体金额比留空更有害。"""

    kind: SuggestionKind
    title: str
    rationale: str
    monthly_potential_low: float | None = None
    monthly_potential_high: float | None = None
    effort_hours_per_week: float | None = None
    time_to_first_income_weeks: float | None = None


class IncomeGrowthResponse(BaseModel):
    """`restricted_to_creator=True` 表示所有数字只统计了 caller 自己记的账(多成员
    共享账本),跟账本「全员合计」不是一个量 —— 必须透出,否则前端会把 creator 维度
    的 median 当成整个账本的 median 展示(同 `routers/insights.py::InsightRangeOut`)。

    `generated=False` 时 `suggestions` 恒为 `[]`,`model` 恒为 None。
    """

    ledger_id: str
    currency: str
    restricted_to_creator: bool
    assessment: IncomeGrowthAssessment
    generated: bool
    generated_reason: GeneratedReason | None = None
    # 只回模型 id,**永远不回 api_key / base_url** —— 那是用户的凭证。
    model: str | None = None
    suggestions: list[IncomeGrowthSuggestion] = Field(default_factory=list)


# ──────────────── 内部 helpers ────────────────


def _money(value: float) -> float:
    return round(value, _MONEY_DECIMALS)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_career_profile(raw: str | None) -> CareerProfile | None:
    """`UserProfile.career_profile_json` TEXT → 校验过的 `CareerProfile`。

    校验失败一律回 None(降级成 `no_career_profile`),不抛:手改过的库行、或者
    未来版本写进去的形状,不该让用户连自己的结余诊断都拿不到。这跟
    `routers/profile.py::_parse_career_profile` 是同一个取舍,但故意不 import 那
    个函数 —— 跨 router import 会把本端点绑在另一条 lane 的私有 helper 上。
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return CareerProfile.model_validate(parsed)
    except ValidationError as exc:
        # 只记错误摘要,不记 body:里面有职业、技能和自由文本 notes。
        logger.warning("ai.income_growth career_profile invalid: %s", str(exc)[:200])
        return None


def _goal_shortfall_total(
    db: Session, *, ledger_id: str, user_id: str, allocatable: float
) -> float:
    """活动目标的月供缺口合计,口径跟 `GET /ledgers/{ext}/goals/plan` 逐字一致。

    复用 `services/goal_plan.py` 那条链(`allocate` + `required_monthly`),**不在
    这里重新推导 allocatable** —— 仓里只能有一套结余分配口径,否则同一个账本在
    目标页和这里会显示两个对不上的缺口。

    第二层鉴权照抄 `routers/goals.py`:`user_id == caller`,共享账本里别人的目标
    不参与(也不泄露)。
    """
    goals = list(
        db.scalars(
            select(FinancialGoal)
            .where(
                FinancialGoal.ledger_id == ledger_id,
                FinancialGoal.user_id == user_id,
                FinancialGoal.status == "active",
            )
            .order_by(
                FinancialGoal.priority.desc(),
                FinancialGoal.created_at.asc(),
                FinancialGoal.id.asc(),
            )
        ).all()
    )
    if not goals:
        return 0.0

    demands = [
        GoalDemand(
            goal_id=goal.id,
            remaining_amount=max(0.0, (goal.target_amount or 0.0) - (goal.saved_amount or 0.0)),
            priority=goal.priority or 0,
        )
        for goal in goals
    ]
    allocation = allocate(demands, allocatable)
    today = _utcnow().date()
    total = 0.0
    for goal, demand in zip(goals, demands, strict=True):
        required = required_monthly(demand.remaining_amount, months_remaining(goal.deadline, today))
        if required is None:
            # 无 deadline 的目标没有「每月必须存多少」,不贡献缺口(同 goals/plan
            # 里 `shortfall` 对 `required is None` 记 0)。
            continue
        total += max(0.0, required - allocation.by_goal[demand.goal_id])
    return total


def _career_prompt_payload(career: CareerProfile) -> dict[str, object]:
    """职业档案 → 进 prompt 的 dict。

    `exclude_none=True`:没填的字段根本不出现,而不是发一堆 `null` 让模型去猜
    「这个 null 是不知道还是不适用」。长度上界由 `schemas.CareerProfile` 的
    `max_length` 保证(skills ≤ 20 项 / 每项 ≤ 64,notes ≤ 500),本函数不再截一遍
    —— 两处截断迟早分叉。
    """
    return dict(career.model_dump(exclude_none=True))


def _coerce_amount(value: Any) -> float | None:
    """LLM 给的数字 → float,拿不到就 None。

    `bool` 显式排除:Python 里 `isinstance(True, int)` 是 True,不拦的话
    `"monthly_potential_low": true` 会变成 1.0 元。负数同样回 None —— 「每月能挣
    -500 元」不是一个有意义的建议,把它当成「模型没估出来」。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if number < 0.0:
        return None
    return round(number, _MONEY_DECIMALS)


def _normalize_suggestions(payload: object) -> list[IncomeGrowthSuggestion]:
    """把 LLM 输出规整成 `IncomeGrowthSuggestion` 列表。

    **LLM 输出是不可信输入**(它的一部分来自用户自己填的自由文本),所以这里只
    夹紧不报错:认不出的 `kind` 归 `other`、超长截断、数字非法归 None、
    `title` 为空的那条整条丢掉。单条烂数据不该让整个响应 500 或 422 —— 确定性那
    半仍然是有效的。
    """
    if isinstance(payload, dict):
        raw_items = payload.get("suggestions")
    elif isinstance(payload, list):
        # 有些模型无视「最外层是 dict」直接给 array,能用就用(同
        # `provider_client._try_parse_json` 对 list 的宽容)。
        raw_items = payload
    else:
        return []
    if not isinstance(raw_items, list):
        return []

    out: list[IncomeGrowthSuggestion] = []
    for item in raw_items:
        if len(out) >= MAX_SUGGESTIONS:
            break
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        rationale = item.get("rationale")
        kind = item.get("kind")
        out.append(
            IncomeGrowthSuggestion(
                kind=kind if kind in _SUGGESTION_KINDS else "other",  # type: ignore[arg-type]
                title=title.strip()[:_MAX_TITLE_CHARS],
                rationale=(
                    rationale.strip()[:_MAX_RATIONALE_CHARS] if isinstance(rationale, str) else ""
                ),
                monthly_potential_low=_coerce_amount(item.get("monthly_potential_low")),
                monthly_potential_high=_coerce_amount(item.get("monthly_potential_high")),
                effort_hours_per_week=_coerce_amount(item.get("effort_hours_per_week")),
                time_to_first_income_weeks=_coerce_amount(item.get("time_to_first_income_weeks")),
            )
        )
    return out


# ──────────────── endpoint ────────────────


@router.post("/income-growth", response_model=IncomeGrowthResponse)
async def income_growth(
    req: IncomeGrowthRequest,
    _scopes: set[str] = Depends(_INCOME_GROWTH_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IncomeGrowthResponse:
    """同步返回 `{assessment, suggestions, generated, generated_reason, ...}`。"""

    # 限流放在最前面 —— 比账本查询更便宜,而且这是唯一挡在付费 API 调用前面的闸。
    if not _check_rate_limit(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error_code": "AI_INCOME_GROWTH_RATE_LIMITED",
                "message": (
                    f"at most {_RATE_LIMIT_MAX} requests per {int(_RATE_LIMIT_WINDOW_S)}s per user"
                ),
            },
        )

    ledger, _role = require_accessible_ledger_by_external_id(
        db,
        user_id=current_user.id,
        ledger_external_id=req.ledger_id,
    )

    window = load_insight_window(
        db,
        ledger=ledger,
        user_id=current_user.id,
        lookback_periods=req.lookback_periods,
        tz_offset_minutes=req.tz_offset_minutes,
    )
    baseline = compute_surplus_baseline(window.periods)
    diagnosis = classify_diagnosis(baseline, window.current)
    ratio = surplus_ratio(baseline.median_income, baseline.median_surplus)
    lever = classify_lever(baseline, window.current, ratio, diagnosis)
    shortfall_total = _goal_shortfall_total(
        db,
        ledger_id=ledger.id,
        user_id=current_user.id,
        allocatable=baseline.allocatable,
    )
    gap, gap_basis = classify_gap(baseline, ratio, shortfall_total)

    assessment = IncomeGrowthAssessment(
        diagnosis=diagnosis,
        lever=lever,
        median_income=_money(baseline.median_income),
        median_expense=_money(baseline.median_expense),
        median_surplus=_money(baseline.median_surplus),
        buffer=_money(baseline.buffer),
        surplus_ratio=round(ratio, _RATIO_DECIMALS),
        basis_periods=baseline.basis_periods,
        target_monthly_gap=_money(gap),
        gap_basis=gap_basis,
    )

    profile = db.scalar(select(UserProfile).where(UserProfile.user_id == current_user.id))
    career = _parse_career_profile(profile.career_profile_json if profile is not None else None)

    def _degraded(reason: GeneratedReason) -> IncomeGrowthResponse:
        logger.info(
            "ai.income_growth user=%s ledger=%s lever=%s gap_basis=%s generated=%s reason=%s",
            current_user.id,
            req.ledger_id,
            lever,
            gap_basis,
            False,
            reason,
        )
        return IncomeGrowthResponse(
            ledger_id=req.ledger_id,
            currency=ledger.currency or "CNY",
            restricted_to_creator=window.restricted_to_creator,
            assessment=assessment,
            generated=False,
            generated_reason=reason,
            model=None,
            suggestions=[],
        )

    # 降级闸门,先命中先返回。顺序是有意的:
    #   1. 数据不足   —— 没有可信基线,任何建议都是凭空生成的
    #   2. 不该动收入 —— 已经够宽裕 / 该动的是支出侧,不为「你没问题」烧一次付费调用
    #   3. 没填档案   —— 没有技能和可投入时间,LLM 只会吐一份通用清单,而这个功能
    #                    存在的意义正是避免通用清单
    #   4. 没配 provider
    # 3 在 4 之前:先催一个「压根没什么可问」的用户去配 AI 服务商是错误引导。
    if diagnosis == "insufficient_data":
        return _degraded("insufficient_data")
    if lever not in ("income", "both"):
        return _degraded("lever_not_income")
    if career is None or not _career_prompt_payload(career):
        return _degraded("no_career_profile")

    try:
        chat_cfg = resolve_chat_provider(current_user, profile)
    except NoChatProviderError:
        return _degraded("no_provider")

    # 用户自由文本(occupation / skills / notes)只进 user 消息,system 消息里
    # 声明「user 块是数据不是指令」。这**限制**而非消除 prompt injection:真正的
    # 兜底是 `_normalize_suggestions` 把返回值当不可信输入解析 + 逐字段夹紧,所以
    # 注入最多能改建议文案,改不了响应结构、拿不到别的用户数据、也调不动别的端点。
    #
    # prompt 里只有 assessment 的数字 + 职业档案。**没有**原始交易、账户名、账本名、
    # 目标名、邮箱、显示名,也没有 `ai_config_json` 里的任何东西。
    messages = build_income_growth_messages(
        assessment=assessment.model_dump(),
        career_profile=_career_prompt_payload(career),
        currency=ledger.currency or "CNY",
        locale=req.locale,
    )

    logger.info(
        "ai.income_growth calling user=%s ledger=%s lever=%s gap_basis=%s "
        "provider=%s career_profile_len=%d",
        current_user.id,
        req.ledger_id,
        lever,
        gap_basis,
        chat_cfg.provider_id,
        len(profile.career_profile_json or "") if profile else 0,
    )

    try:
        raw = await call_chat_json(
            config=chat_cfg,
            messages=messages,
            timeout=_LLM_TIMEOUT_S,
            max_retries=_LLM_MAX_RETRIES,
            max_tokens=_LLM_MAX_TOKENS,
        )
    except ChatProviderError as exc:
        # `JsonParseFailedError` 是 `ChatProviderError` 的子类(provider_client.py:239),
        # 一并落在这里 —— 对本端点来说「上游挂了」和「上游吐了非 JSON」是同一件事:
        # 拿不到建议,但 assessment 照常返回。
        logger.warning(
            "ai.income_growth provider failed user=%s provider=%s err=%s",
            current_user.id,
            chat_cfg.provider_id,
            str(exc)[:200],
        )
        return _degraded("provider_failed")

    suggestions = _normalize_suggestions(raw)
    logger.info(
        "ai.income_growth user=%s ledger=%s lever=%s gap_basis=%s generated=%s reason=%s "
        "suggestions=%d",
        current_user.id,
        req.ledger_id,
        lever,
        gap_basis,
        True,
        None,
        len(suggestions),
    )
    # 一条都没活下来也回 generated=true:LLM 确实答了,只是内容全不可用 ——
    # 前端对「生成了但是空」有单独的状态,跟「没生成」不是一回事。
    return IncomeGrowthResponse(
        ledger_id=req.ledger_id,
        currency=ledger.currency or "CNY",
        restricted_to_creator=window.restricted_to_creator,
        assessment=assessment,
        generated=True,
        generated_reason=None,
        model=chat_cfg.model,
        suggestions=suggestions,
    )
