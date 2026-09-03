"""目标可行性 + 结余分配的契约测试。

两层,跟实现的两层对齐:

1. 纯函数层(`src/services/goal_plan.py`)—— 不起 app、不建表、不造 token,
   直接喂数字:权重分配 / 全 0 权重平分 / 封顶重分配 / 未分配余额 /
   `months_remaining == 0` 不除零 / 4 个 feasibility 分支。
2. 端点层(`GET /api/v1/ledgers/{ext}/goals/plan`)—— boilerplate 沿用
   `tests/test_goals_crud.py` 的 `_make_client` / `_register` / `_login_web` /
   `_seed_ledger`,交易直接写 `read_tx_projection`(读路径唯一权威源),
   不走 sync/push:不写 `ledger_snapshot`,不插 `sync_changes`。

端点测试把 `routers.goals._utcnow` 冻在一个固定日期上 —— `months_remaining`
是「从今天到 deadline 的整月数」,不冻住就得在测试里把实现的日期算法抄一遍,
那等于用实现验证实现。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.models import Ledger, ReadTxProjection
from src.routers import goals as goals_router
from src.services.goal_plan import (
    GoalDemand,
    allocate,
    classify_feasibility,
    months_remaining,
    projected_months,
    required_monthly,
)

_PASSWORD = "123456"

# 冻结的「今天」。所有 deadline 都用 15 号,跟它同一个日,整月数因此不受
# 月末夹日影响(见 `months_remaining` docstring)。
_FROZEN_NOW = datetime(2030, 3, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 纯函数:分配
# ---------------------------------------------------------------------------


def test_allocate_splits_by_priority_weight() -> None:
    """权重 30 : 10 → 额度按 3 : 1 分。"""
    demands = [
        GoalDemand(goal_id="a", remaining_amount=100_000.0, priority=30),
        GoalDemand(goal_id="b", remaining_amount=100_000.0, priority=10),
    ]

    result = allocate(demands, 2000.0)

    assert result.by_goal == {"a": pytest.approx(1500.0), "b": pytest.approx(500.0)}


def test_allocate_splits_equally_when_all_priorities_are_zero() -> None:
    """全员没填权重 → 平分,服务端不替用户猜一个偏好。"""
    demands = [
        GoalDemand(goal_id="a", remaining_amount=10_000.0, priority=0),
        GoalDemand(goal_id="b", remaining_amount=10_000.0, priority=0),
        GoalDemand(goal_id="c", remaining_amount=10_000.0, priority=0),
    ]

    result = allocate(demands, 900.0)

    assert result.by_goal == {
        "a": pytest.approx(300.0),
        "b": pytest.approx(300.0),
        "c": pytest.approx(300.0),
    }


def test_allocate_caps_goal_at_remaining_and_redistributes_excess() -> None:
    """a 按权重该拿 1500 但只差 100 → 多出来的 1400 全部重分给 b。"""
    demands = [
        GoalDemand(goal_id="a", remaining_amount=100.0, priority=30),
        GoalDemand(goal_id="b", remaining_amount=100_000.0, priority=10),
    ]

    result = allocate(demands, 2000.0)

    assert result.by_goal == {"a": pytest.approx(100.0), "b": pytest.approx(1900.0)}


def test_allocate_mixed_priorities_redistribute_to_zero_weight_goal() -> None:
    """有权重的先吃,封顶后剩下的落到 0 权重的目标上,而不是停在 unallocated。"""
    demands = [
        GoalDemand(goal_id="weighted", remaining_amount=100.0, priority=30),
        GoalDemand(goal_id="unweighted", remaining_amount=10_000.0, priority=0),
    ]

    result = allocate(demands, 2000.0)

    assert result.by_goal == {"weighted": pytest.approx(100.0), "unweighted": pytest.approx(1900.0)}
    assert result.unallocated == pytest.approx(0.0)


def test_allocate_reports_unallocated_when_goals_need_less_than_allocatable() -> None:
    """目标总缺口 800 < 可分配 2000 → 剩下的 1200 进 unallocated,不硬塞。"""
    demands = [
        GoalDemand(goal_id="a", remaining_amount=500.0, priority=30),
        GoalDemand(goal_id="b", remaining_amount=300.0, priority=10),
    ]

    result = allocate(demands, 2000.0)

    assert result.unallocated == pytest.approx(1200.0)


def test_allocate_gives_nothing_to_fully_funded_goal() -> None:
    """已攒满的目标不参与分配 —— 额度全给还差钱的那个。"""
    demands = [
        GoalDemand(goal_id="funded", remaining_amount=0.0, priority=100),
        GoalDemand(goal_id="open", remaining_amount=9000.0, priority=1),
    ]

    result = allocate(demands, 2000.0)

    assert result.by_goal == {"funded": 0.0, "open": pytest.approx(2000.0)}


def test_allocate_returns_everything_unallocated_without_goals() -> None:
    """一个目标都没有时不能吞掉额度,也不能报错。"""
    result = allocate([], 2000.0)

    assert (dict(result.by_goal), result.unallocated) == ({}, pytest.approx(2000.0))


# ---------------------------------------------------------------------------
# 纯函数:期限 / 月供 / 预计月数
# ---------------------------------------------------------------------------


def test_months_remaining_counts_whole_months() -> None:
    assert months_remaining(date(2031, 1, 15), date(2030, 3, 15)) == 10


def test_months_remaining_floors_at_zero_for_past_deadline() -> None:
    """deadline 已经过去 → 0,不回负数让调用方去猜。"""
    assert months_remaining(date(2030, 1, 15), date(2030, 3, 15)) == 0


def test_months_remaining_is_none_without_deadline() -> None:
    assert months_remaining(None, date(2030, 3, 15)) is None


def test_required_monthly_due_now_does_not_divide_by_zero() -> None:
    """本月内到期(整月数 0)且还差 800 → 整笔 800 现在就要到位。"""
    assert required_monthly(800.0, 0) == pytest.approx(800.0)


def test_required_monthly_is_none_without_deadline() -> None:
    assert required_monthly(800.0, None) is None


def test_projected_months_rounds_up() -> None:
    """10000 / 1500 = 6.67 → 7 个月(不是 6,6 个月还差 1000)。"""
    assert projected_months(10_000.0, 1500.0) == 7


def test_projected_months_is_none_without_allocation() -> None:
    """一分钱都分不到 → 没有有限的「还要几个月」。"""
    assert projected_months(10_000.0, 0.0) is None


# ---------------------------------------------------------------------------
# 纯函数:4 个 feasibility 分支
# ---------------------------------------------------------------------------


def test_feasibility_no_deadline_when_goal_has_no_deadline() -> None:
    assert classify_feasibility(5000.0, None, 800.0) == "no_deadline"


def test_feasibility_infeasible_when_allocation_below_required() -> None:
    assert classify_feasibility(10_000.0, 1000.0, 999.0) == "infeasible"


def test_feasibility_tight_when_allocation_barely_covers_required() -> None:
    """1000 / 1000 刚好够 —— 余量 0,一次超支就穿,不能报 feasible。"""
    assert classify_feasibility(10_000.0, 1000.0, 1000.0) == "tight"


def test_feasibility_feasible_when_allocation_has_slack() -> None:
    """required 占 allocated 的 66% < _TIGHT_RATIO(0.9)→ 宽裕。"""
    assert classify_feasibility(10_000.0, 1000.0, 1500.0) == "feasible"


def test_feasibility_feasible_when_goal_already_funded() -> None:
    """已攒满是终态:即使没有 deadline 也回 feasible,不回 no_deadline。"""
    assert classify_feasibility(0.0, None, 0.0) == "feasible"


# ---------------------------------------------------------------------------
# 端点 boilerplate(沿用 tests/test_goals_crud.py)
# ---------------------------------------------------------------------------


def _make_client() -> tuple[TestClient, sessionmaker]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app), TS


def _register(client: TestClient, email: str) -> dict:
    res = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": _PASSWORD,
            "client_type": "web",
            "device_name": "pytest-web",
            "platform": "web",
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


def _login_web(client: TestClient, email: str) -> dict:
    res = client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "password": _PASSWORD,
            "client_type": "web",
            "device_name": "pytest-web",
            "platform": "web",
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_ledger(client: TestClient, token: str, ledger_id: str) -> None:
    res = client.post(
        "/api/v1/write/ledgers",
        headers=_auth(token),
        json={"ledger_id": ledger_id, "ledger_name": ledger_id, "currency": "CNY"},
    )
    assert res.status_code == 200, res.text


def _share_ledger(
    client: TestClient, *, owner_token: str, member_token: str, ledger_id: str
) -> None:
    res = client.post(
        f"/api/v1/ledgers/{ledger_id}/invites",
        headers=_auth(owner_token),
        json={"role": "editor", "expires_in_hours": 24},
    )
    assert res.status_code == 201, res.text
    code = res.json()["code"]
    res = client.post(f"/api/v1/invites/{code}/accept", headers=_auth(member_token))
    assert res.status_code == 200, res.text


def _months_ago(now: datetime, months: int) -> datetime:
    """`months` 个月前那个月的 6 号 12:00 UTC —— 任何月份都落在月内。"""
    total = now.year * 12 + (now.month - 1) - months
    return datetime(total // 12, total % 12 + 1, 6, 12, 0, tzinfo=timezone.utc)


def _seed_three_completed_months(TS: sessionmaker, ledger_id: str) -> None:
    """前 3 个完整月:收入固定 5000,支出 1000 / 2000 / 3000。

    → median_income=5000,median_expense=2000,median_surplus=3000,
      buffer=MAD([1000,2000,3000])=1000,allocatable=2000。
    直接写 `read_tx_projection`(读路径唯一权威源),不走 sync/push。
    """
    now = datetime.now(timezone.utc)
    db = TS()
    try:
        ledger = db.scalars(select(Ledger).where(Ledger.external_id == ledger_id)).one()
        for months, expense in ((3, 1000.0), (2, 2000.0), (1, 3000.0)):
            happened = _months_ago(now, months)
            db.add(
                ReadTxProjection(
                    ledger_id=ledger.id,
                    sync_id=f"tx-income-{months}",
                    user_id=ledger.user_id,
                    tx_type="income",
                    amount=5000.0,
                    happened_at=happened,
                    category_name="Salary",
                    created_by_user_id=ledger.user_id,
                )
            )
            db.add(
                ReadTxProjection(
                    ledger_id=ledger.id,
                    sync_id=f"tx-expense-{months}",
                    user_id=ledger.user_id,
                    tx_type="expense",
                    amount=expense,
                    happened_at=happened,
                    category_name="Food",
                    created_by_user_id=ledger.user_id,
                )
            )
        db.commit()
    finally:
        db.close()


def _create_goal(client: TestClient, token: str, ledger_id: str, **payload) -> dict:
    res = client.post(
        f"/api/v1/ledgers/{ledger_id}/goals",
        headers=_auth(token),
        json=payload,
    )
    assert res.status_code == 201, res.text
    return res.json()


# ---------------------------------------------------------------------------
# 端点契约
# ---------------------------------------------------------------------------


def test_goals_plan_allocates_surplus_by_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """可分配 2000 按权重 30 : 10 分给 Laptop / Trip,已攒满和 archived 的不吃额度。"""
    client, TS = _make_client()
    monkeypatch.setattr(goals_router, "_utcnow", lambda: _FROZEN_NOW)
    try:
        email = "goal-plan-happy@example.com"
        _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_GOAL_PLAN"
        _seed_ledger(client, token, ledger_id)
        _seed_three_completed_months(TS, ledger_id)

        _create_goal(
            client,
            token,
            ledger_id,
            name="Funded",
            target_amount=1000.0,
            saved_amount=1000.0,
            priority=100,
            deadline="2030-12-15",
        )
        _create_goal(
            client,
            token,
            ledger_id,
            name="Laptop",
            target_amount=15000.0,
            saved_amount=5000.0,
            priority=30,
            deadline="2031-01-15",
        )
        _create_goal(
            client,
            token,
            ledger_id,
            name="Trip",
            target_amount=3000.0,
            priority=10,
            deadline="2030-09-15",
        )
        _create_goal(client, token, ledger_id, name="Someday", target_amount=2000.0)
        _create_goal(
            client,
            token,
            ledger_id,
            name="Archived",
            target_amount=9999.0,
            priority=50,
            status="archived",
        )

        res = client.get(
            f"/api/v1/ledgers/{ledger_id}/goals/plan",
            headers=_auth(token),
            params={"lookback_periods": 4},
        )
        assert res.status_code == 200, res.text
        body = res.json()

        assert body["baseline"] == {
            "median_income": 5000.0,
            "median_expense": 2000.0,
            "median_surplus": 3000.0,
            "buffer": 1000.0,
            "allocatable": 2000.0,
            "basis_periods": 3,
        }
        assert body["restricted_to_creator"] is False
        # archived 不进 items;顺序按 priority 降序
        assert [item["name"] for item in body["items"]] == [
            "Funded",
            "Laptop",
            "Trip",
            "Someday",
        ]
        funded, laptop, trip, someday = body["items"]
        # 已攒满:不吃额度,也不谈可行性
        assert (funded["allocated_monthly"], funded["feasibility"]) == (0.0, "feasible")
        assert funded["projected_months"] is None
        # 10 个整月 × 1000 = 缺口 10000;分到 1500,余量 50% → feasible
        assert (laptop["months_remaining"], laptop["required_monthly"]) == (10, 1000.0)
        assert (laptop["allocated_monthly"], laptop["shortfall"]) == (1500.0, 0.0)
        assert (laptop["projected_months"], laptop["feasibility"]) == (7, "feasible")
        # 6 个整月 × 500 = 缺口 3000;分到 500 刚好够 → tight
        assert (trip["required_monthly"], trip["allocated_monthly"]) == (500.0, 500.0)
        assert (trip["projected_months"], trip["feasibility"]) == (6, "tight")
        # 无 deadline:权重 0 分到 0,不回月供也不回 shortfall
        assert someday["months_remaining"] is None
        assert someday["required_monthly"] is None
        assert (someday["shortfall"], someday["feasibility"]) == (0.0, "no_deadline")
        # 1000(Laptop) + 500(Trip) + 0(Funded);额度刚好分光
        assert (body["allocatable"], body["total_required_monthly"]) == (2000.0, 1500.0)
        assert body["unallocated"] == 0.0
    finally:
        app.dependency_overrides.clear()


def test_goals_plan_lookback_periods_out_of_range_rejected() -> None:
    client, _TS = _make_client()
    try:
        email = "goal-plan-range@example.com"
        _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_GOAL_PLAN_RANGE"
        _seed_ledger(client, token, ledger_id)

        for value in (2, 25):
            res = client.get(
                f"/api/v1/ledgers/{ledger_id}/goals/plan",
                headers=_auth(token),
                params={"lookback_periods": value},
            )
            assert res.status_code == 422, f"{value} → {res.status_code}"
    finally:
        app.dependency_overrides.clear()


def test_goals_plan_hides_other_members_goals() -> None:
    """共享账本第二层鉴权:B 进得来账本(200),但 A 的目标不进 B 的 plan。"""
    client, TS = _make_client()
    try:
        owner_email = "goal-plan-owner@example.com"
        member_email = "goal-plan-member@example.com"
        _register(client, owner_email)
        _register(client, member_email)
        owner_token = _login_web(client, owner_email)["access_token"]
        member_token = _login_web(client, member_email)["access_token"]
        ledger_id = "L_GOAL_PLAN_SHARED"
        _seed_ledger(client, owner_token, ledger_id)
        _share_ledger(
            client,
            owner_token=owner_token,
            member_token=member_token,
            ledger_id=ledger_id,
        )
        _seed_three_completed_months(TS, ledger_id)
        _create_goal(
            client,
            owner_token,
            ledger_id,
            name="Owner only",
            target_amount=15000.0,
            priority=10,
        )

        res = client.get(
            f"/api/v1/ledgers/{ledger_id}/goals/plan",
            headers=_auth(member_token),
            params={"lookback_periods": 4},
        )
        assert res.status_code == 200, res.text
        body = res.json()

        assert body["items"] == []
        # 多成员账本按 caller 归属统计:B 没记过账 → 基线全 0,额度也是 0
        assert body["restricted_to_creator"] is True
        assert (body["allocatable"], body["unallocated"]) == (0.0, 0.0)
        # A 自己看得到,数字不受影响
        res = client.get(
            f"/api/v1/ledgers/{ledger_id}/goals/plan",
            headers=_auth(owner_token),
            params={"lookback_periods": 4},
        )
        assert [item["name"] for item in res.json()["items"]] == ["Owner only"]
    finally:
        app.dependency_overrides.clear()
