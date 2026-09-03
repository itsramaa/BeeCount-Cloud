"""账本财务洞察端点契约测试(`GET /api/v1/ledgers/{id}/insights`)。

覆盖:
1. 基线 / series 只建立在**已完成**周期上(当前半个周期不参与 median)
2. 分类基线 + 预算建议(建议额 = 分类 median,无系数)
3. 两个排除标记互不串味:`exclude_from_budget` 只动预算 used,
   `exclude_from_stats` 只动收支统计
4. 单成员账本里 `created_by_user_id` 为 NULL 的老行仍全量统计
5. 多成员共享账本按 caller 归属过滤,并把 `restricted_to_creator` 透出
6. `lookback_periods` 越界 → 422
7. 跨用户隔离:非成员拿 404(不是 403,不泄露账本存在性)

boilerplate 沿用 `tests/test_budget_usage_exclude_flags.py` 的
`_make_client` / `_register` / `_login_web` / `_seed_ledger` / `_push_tx`。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.models import Ledger, ReadTxProjection

_PASSWORD = "123456"


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
            "client_type": "app",
            "device_name": "pytest-app",
            "platform": "app",
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


def _seed_ledger(client: TestClient, token: str, device_id: str, ledger_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    content = (
        f'{{"ledgerName":"{ledger_id}","currency":"CNY","count":0,'
        '"items":[],"accounts":[],"categories":[],"tags":[]}'
    )
    res = client.post(
        "/api/v1/sync/push",
        headers=_auth(token),
        json={
            "device_id": device_id,
            "changes": [
                {
                    "ledger_id": ledger_id,
                    "entity_type": "ledger_snapshot",
                    "entity_sync_id": ledger_id,
                    "action": "upsert",
                    "payload": {"content": content},
                    "updated_at": now,
                }
            ],
        },
    )
    assert res.status_code == 200, res.text


def _latest_change_id(client: TestClient, token: str, ledger_id: str) -> int:
    res = client.get(f"/api/v1/read/ledgers/{ledger_id}", headers=_auth(token))
    assert res.status_code == 200, res.text
    return int(res.json()["source_change_id"])


def _push_tx(client: TestClient, token: str, device_id: str, ledger_id: str, payload: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    res = client.post(
        "/api/v1/sync/push",
        headers=_auth(token),
        json={
            "device_id": device_id,
            "changes": [
                {
                    "ledger_id": ledger_id,
                    "entity_type": "transaction",
                    "entity_sync_id": payload["syncId"],
                    "action": "upsert",
                    "payload": payload,
                    "updated_at": now,
                }
            ],
        },
    )
    assert res.status_code == 200, res.text


def _share_ledger(
    client: TestClient, *, owner_token: str, member_token: str, ledger_id: str
) -> None:
    """真共享:owner 发邀请码 → member 接受 → 双方都是 LedgerMember。"""
    res = client.post(
        f"/api/v1/ledgers/{ledger_id}/invites",
        headers=_auth(owner_token),
        json={"role": "editor", "expires_in_hours": 24},
    )
    assert res.status_code == 201, res.text
    code = res.json()["code"]
    res = client.post(f"/api/v1/invites/{code}/accept", headers=_auth(member_token))
    assert res.status_code == 200, res.text


def _insights(client: TestClient, token: str, ledger_id: str, **params) -> dict:
    res = client.get(f"/api/v1/ledgers/{ledger_id}/insights", headers=_auth(token), params=params)
    assert res.status_code == 200, res.text
    return res.json()


def _period_label(moment: datetime) -> str:
    """month_start_day=1(自然月)下的周期标签月,同 `_bucket_key(scope="year")`。"""
    return moment.strftime("%Y-%m")


def _months_ago(now: datetime, months: int) -> datetime:
    """`months` 个月前那个月的 6 号 12:00 UTC —— 任何月份都落在月内。"""
    total = now.year * 12 + (now.month - 1) - months
    return datetime(total // 12, total % 12 + 1, 6, 12, 0, tzinfo=timezone.utc)


def _seed_three_completed_months(
    client: TestClient, token: str, device: str, ledger_id: str, now: datetime
) -> None:
    """前 3 个完整月:收入固定 5000,支出 1000 / 2000 / 3000(Food 60% + Transport 40%)。

    → median_expense=2000,median_income=5000,median_surplus=3000,
      buffer=MAD([1000,2000,3000])=1000,allocatable=2000。
    当前周期再放一笔 100(Food),用于验证它不参与基线。
    """
    for months, expense in ((3, 1000.0), (2, 2000.0), (1, 3000.0)):
        happened = _months_ago(now, months).isoformat()
        _push_tx(
            client,
            token,
            device,
            ledger_id,
            {
                "syncId": f"tx-income-{months}",
                "type": "income",
                "amount": 5000.0,
                "happenedAt": happened,
                "categoryName": "Salary",
            },
        )
        _push_tx(
            client,
            token,
            device,
            ledger_id,
            {
                "syncId": f"tx-food-{months}",
                "type": "expense",
                "amount": expense * 0.6,
                "happenedAt": happened,
                "categoryName": "Food",
            },
        )
        _push_tx(
            client,
            token,
            device,
            ledger_id,
            {
                "syncId": f"tx-transport-{months}",
                "type": "expense",
                "amount": expense * 0.4,
                "happenedAt": happened,
                "categoryName": "Transport",
            },
        )
    _push_tx(
        client,
        token,
        device,
        ledger_id,
        {
            "syncId": "tx-current",
            "type": "expense",
            "amount": 100.0,
            "happenedAt": now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat(),
            "categoryName": "Food",
        },
    )


def test_insights_baseline_uses_only_completed_periods() -> None:
    client, _TS = _make_client()
    try:
        owner = _register(client, "insight-baseline@example.com")
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_BASE"
        _seed_ledger(client, token, device, ledger_id)
        now = datetime.now(timezone.utc)
        _seed_three_completed_months(client, token, device, ledger_id, now)

        # lookback_periods=4 = 3 个已完成周期 + 当前周期
        body = _insights(client, token, ledger_id, lookback_periods=4)

        assert [item["bucket"] for item in body["series"]] == [
            _period_label(_months_ago(now, 3)),
            _period_label(_months_ago(now, 2)),
            _period_label(_months_ago(now, 1)),
        ]
        assert body["baseline"] == {
            "median_income": 5000.0,
            "median_expense": 2000.0,
            "median_surplus": 3000.0,
            "buffer": 1000.0,
            "allocatable": 2000.0,
        }
        assert body["range"]["basis_periods"] == 3
        # 当前周期那笔 100 只进 current,不进基线
        assert body["current"]["expense"] == 100.0
        assert body["current"]["bucket"] == _period_label(now)
    finally:
        app.dependency_overrides.clear()


def test_insights_zero_fills_periods_without_transactions() -> None:
    client, _TS = _make_client()
    try:
        owner = _register(client, "insight-zerofill@example.com")
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_ZERO"
        _seed_ledger(client, token, device, ledger_id)
        now = datetime.now(timezone.utc)
        _seed_three_completed_months(client, token, device, ledger_id, now)

        # 窗口拉到 7 个周期 → 前 3 个月没有任何交易,必须补 0(不是跳过),
        # 否则 median 只反映「有记账的月」,基线系统性偏高。
        body = _insights(client, token, ledger_id, lookback_periods=7)

        assert [item["expense"] for item in body["series"]] == [
            0.0,
            0.0,
            0.0,
            1000.0,
            2000.0,
            3000.0,
        ]
        assert body["baseline"]["median_expense"] == 500.0
    finally:
        app.dependency_overrides.clear()


def test_insights_category_baseline_and_recommendation() -> None:
    client, _TS = _make_client()
    try:
        owner = _register(client, "insight-category@example.com")
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_CAT"
        _seed_ledger(client, token, device, ledger_id)
        now = datetime.now(timezone.utc)
        _seed_three_completed_months(client, token, device, ledger_id, now)

        body = _insights(client, token, ledger_id, lookback_periods=4)

        # Food: [600, 1200, 1800] → median 1200;median 降序排在 Transport 之前
        assert body["category_baselines"][0] == {
            "category_name": "Food",
            "median": pytest.approx(1200.0),
            "mean": pytest.approx(1200.0),
            "periods_present": 3,
        }
        # 建议额 = 分类 median,不乘任何系数
        assert body["recommendation"]["categories"] == [
            {
                "category_name": "Food",
                "recommended_amount": pytest.approx(1200.0),
                "basis_periods": 3,
            },
            {
                "category_name": "Transport",
                "recommended_amount": pytest.approx(800.0),
                "basis_periods": 3,
            },
        ]
        assert body["recommendation"]["saving_amount"] == 2000.0
        assert body["recommendation"]["buffer_amount"] == 1000.0
    finally:
        app.dependency_overrides.clear()


def test_insights_diagnosis_on_track_with_healthy_surplus() -> None:
    client, _TS = _make_client()
    try:
        owner = _register(client, "insight-diagnosis@example.com")
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_DIAG"
        _seed_ledger(client, token, device, ledger_id)
        now = datetime.now(timezone.utc)
        _seed_three_completed_months(client, token, device, ledger_id, now)

        body = _insights(client, token, ledger_id, lookback_periods=4)

        # 3 个样本 + median_income>0 + 当前支出没超 1.2 倍基线 + 结余率 0.6
        assert body["diagnosis"] == "on_track"
    finally:
        app.dependency_overrides.clear()


def _seed_current_period_flag_mix(
    client: TestClient, token: str, device: str, ledger_id: str
) -> None:
    """当前周期三笔:普通 100 / exclude_from_budget 500 / exclude_from_stats 30。"""
    happened = (
        datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    )
    _push_tx(
        client,
        token,
        device,
        ledger_id,
        {"syncId": "tx-normal", "type": "expense", "amount": 100.0, "happenedAt": happened},
    )
    _push_tx(
        client,
        token,
        device,
        ledger_id,
        {
            "syncId": "tx-excl-budget",
            "type": "expense",
            "amount": 500.0,
            "happenedAt": happened,
            "excludeFromBudget": True,
        },
    )
    _push_tx(
        client,
        token,
        device,
        ledger_id,
        {
            "syncId": "tx-excl-stats",
            "type": "expense",
            "amount": 30.0,
            "happenedAt": happened,
            "excludeFromStats": True,
            "excludeFromBudget": False,
        },
    )


def _create_total_budget(client: TestClient, web_token: str, ledger_id: str, amount: float) -> None:
    res = client.post(
        f"/api/v1/write/ledgers/{ledger_id}/budgets",
        headers=_auth(web_token),
        json={
            "base_change_id": _latest_change_id(client, web_token, ledger_id),
            "type": "total",
            "amount": amount,
            "start_day": 1,
        },
    )
    assert res.status_code == 200, res.text


def test_insights_budget_used_ignores_exclude_from_stats() -> None:
    client, _TS = _make_client()
    try:
        email = "insight-budget-flag@example.com"
        owner = _register(client, email)
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_BUDGET"
        _seed_ledger(client, token, device, ledger_id)
        _seed_current_period_flag_mix(client, token, device, ledger_id)
        web_token = _login_web(client, email)["access_token"]
        _create_total_budget(client, web_token, ledger_id, 5000.0)

        status = _insights(client, web_token, ledger_id)["budget_status"]

        # 预算 used 只看 exclude_from_budget:500 被排除,30 仍计入 → 130
        assert len(status) == 1
        assert status[0]["used"] == 130.0
        assert status[0]["remaining"] == 4870.0
        assert status[0]["percent_used"] == 2.6
        assert status[0]["exceeded"] is False
    finally:
        app.dependency_overrides.clear()


def test_insights_current_expense_ignores_exclude_from_budget() -> None:
    client, _TS = _make_client()
    try:
        email = "insight-stats-flag@example.com"
        owner = _register(client, email)
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_STATS"
        _seed_ledger(client, token, device, ledger_id)
        _seed_current_period_flag_mix(client, token, device, ledger_id)

        current = _insights(client, token, ledger_id)["current"]

        # 收支统计只看 exclude_from_stats:30 被排除,exclude_from_budget 的
        # 500 仍计入 → 100 + 500 = 600(与上一条测试的 130 正好互补)
        assert current["expense"] == 600.0
    finally:
        app.dependency_overrides.clear()


def test_insights_safe_daily_spreads_remaining_over_days_left() -> None:
    client, _TS = _make_client()
    try:
        email = "insight-safe-daily@example.com"
        owner = _register(client, email)
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_SAFE"
        _seed_ledger(client, token, device, ledger_id)
        _seed_current_period_flag_mix(client, token, device, ledger_id)
        web_token = _login_web(client, email)["access_token"]
        _create_total_budget(client, web_token, ledger_id, 5000.0)

        body = _insights(client, web_token, ledger_id)

        days_remaining = body["current"]["days_remaining"]
        remaining = body["budget_status"][0]["remaining"]
        expected = remaining / days_remaining if days_remaining > 0 else remaining
        assert body["budget_status"][0]["safe_daily"] == pytest.approx(expected)
        assert body["current"]["days_elapsed"] + days_remaining == body["current"]["days_total"]
    finally:
        app.dependency_overrides.clear()


def test_insights_single_member_ledger_counts_null_creator_rows() -> None:
    client, TS = _make_client()
    try:
        owner = _register(client, "insight-nullcreator@example.com")
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_NULL"
        _seed_ledger(client, token, device, ledger_id)

        # 老服务端写入的行 created_by_user_id 是 NULL(push 兜底注入
        # updatedByUserId 是共享账本修复之后才有的)。单成员账本上按
        # created_by_user_id 过滤会把这些行全滤掉 → 自信的错数。
        db = TS()
        try:
            ledger = db.scalars(select(Ledger).where(Ledger.external_id == ledger_id)).one()
            now = datetime.now(timezone.utc)
            db.add(
                ReadTxProjection(
                    ledger_id=ledger.id,
                    sync_id="tx-legacy-null-creator",
                    user_id=ledger.user_id,
                    tx_type="expense",
                    amount=250.0,
                    happened_at=now.replace(hour=12, minute=0, second=0, microsecond=0),
                    category_name="Food",
                    created_by_user_id=None,
                )
            )
            db.commit()
        finally:
            db.close()

        body = _insights(client, token, ledger_id)

        assert body["range"]["restricted_to_creator"] is False
        assert body["current"]["expense"] == 250.0
    finally:
        app.dependency_overrides.clear()


def test_insights_shared_ledger_restricts_to_caller_rows() -> None:
    client, _TS = _make_client()
    try:
        owner_email = "insight-shared-owner@example.com"
        member_email = "insight-shared-member@example.com"
        owner = _register(client, owner_email)
        member = _register(client, member_email)
        owner_token, owner_device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_SHARED"
        _seed_ledger(client, owner_token, owner_device, ledger_id)
        _share_ledger(
            client,
            owner_token=_login_web(client, owner_email)["access_token"],
            member_token=_login_web(client, member_email)["access_token"],
            ledger_id=ledger_id,
        )
        _seed_current_period_flag_mix(client, owner_token, owner_device, ledger_id)

        member_body = _insights(client, member["access_token"], ledger_id)
        owner_body = _insights(client, owner_token, ledger_id)

        # 多成员账本按 created_by_user_id 归属:member 没记过账 → 0,
        # owner 拿到自己那 600(100 + exclude_from_budget 的 500)
        assert member_body["range"]["restricted_to_creator"] is True
        assert member_body["current"]["expense"] == 0.0
        assert owner_body["current"]["expense"] == 600.0
    finally:
        app.dependency_overrides.clear()


def test_insights_lookback_periods_out_of_range_rejected() -> None:
    client, _TS = _make_client()
    try:
        owner = _register(client, "insight-range@example.com")
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_RANGE"
        _seed_ledger(client, token, device, ledger_id)

        for value in (2, 25):
            res = client.get(
                f"/api/v1/ledgers/{ledger_id}/insights",
                headers=_auth(token),
                params={"lookback_periods": value},
            )
            assert res.status_code == 422, f"{value} → {res.status_code}"
    finally:
        app.dependency_overrides.clear()


def test_insights_cross_user_isolation_returns_404() -> None:
    client, _TS = _make_client()
    try:
        owner = _register(client, "insight-a@example.com")
        outsider = _register(client, "insight-b@example.com")
        ledger_id = "L_INSIGHT_PRIVATE"
        _seed_ledger(client, owner["access_token"], owner["device_id"], ledger_id)

        res = client.get(
            f"/api/v1/ledgers/{ledger_id}/insights",
            headers=_auth(outsider["access_token"]),
        )

        # 非成员一律 404,不是 403 —— 不泄露账本存在性
        assert res.status_code == 404, res.text
    finally:
        app.dependency_overrides.clear()


def test_insights_empty_ledger_reports_insufficient_data() -> None:
    client, _TS = _make_client()
    try:
        owner = _register(client, "insight-empty@example.com")
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_EMPTY"
        _seed_ledger(client, token, device, ledger_id)

        body = _insights(client, token, ledger_id)

        # 没有任何交易时读端点不能 500,也不能给出自信的结论
        assert body["diagnosis"] == "insufficient_data"
        assert body["baseline"]["median_expense"] == 0.0
        assert body["budget_status"] == []
    finally:
        app.dependency_overrides.clear()


def test_insights_window_bounds_cover_lookback_periods() -> None:
    client, _TS = _make_client()
    try:
        owner = _register(client, "insight-window@example.com")
        token, device = owner["access_token"], owner["device_id"]
        ledger_id = "L_INSIGHT_WINDOW"
        _seed_ledger(client, token, device, ledger_id)
        now = datetime.now(timezone.utc)

        body = _insights(client, token, ledger_id, lookback_periods=6)

        # 窗口 = 6 个周期,结束在当前周期末(下月起始日),起点在 5 个月前
        start_at = datetime.fromisoformat(body["range"]["start_at"])
        end_at = datetime.fromisoformat(body["range"]["end_at"])
        assert _period_label(start_at) == _period_label(_months_ago(now, 5))
        assert _period_label(end_at) == _period_label(_months_ago(now, -1))
        assert start_at.day == 1 and end_at.day == 1
        assert timedelta(days=150) < end_at - start_at < timedelta(days=190)
    finally:
        app.dependency_overrides.clear()
