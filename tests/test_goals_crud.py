"""攒钱目标 CRUD 契约测试(server-only 实体,不走同步层)。

覆盖:
1. CRUD 往返:create → list → patch saved_amount → delete
2. 边界校验拒绝:target_amount <= 0 / deadline 在过去 / name 全空白
3. 跨用户隔离:**真共享账本**里成员 B 既看不到也改不了成员 A 的目标(404)

boilerplate 沿用 tests/test_budget_usage_exclude_flags.py 的
_make_client / _register / _login_web,账本用 web 写端点直接建
(POST /write/ledgers 会顺手写 owner 的 LedgerMember 行)。
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app

_PASSWORD = "123456"


def _make_client() -> TestClient:
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
    return TestClient(app)


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
    assert res.json()["role"] == "editor"


def _create_goal(client: TestClient, token: str, ledger_id: str, **overrides) -> dict:
    payload: dict = {
        "name": "Laptop",
        "target_amount": 15000.0,
        "deadline": (date.today() + timedelta(days=300)).isoformat(),
        "priority": 10,
    }
    payload.update(overrides)
    res = client.post(
        f"/api/v1/ledgers/{ledger_id}/goals",
        headers=_auth(token),
        json=payload,
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_goal_crud_roundtrip() -> None:
    """create → list → patch saved_amount → delete,派生量每步都跟着走。"""
    client = _make_client()
    try:
        _register(client, "goals-crud@example.com")
        token = _login_web(client, "goals-crud@example.com")["access_token"]
        ledger_id = "L_GOALS_CRUD"
        _seed_ledger(client, token, ledger_id)

        created = _create_goal(client, token, ledger_id)
        assert created["saved_amount"] == 0.0
        assert created["remaining_amount"] == 15000.0
        assert created["progress_pct"] == 0.0
        assert created["currency"] == "CNY"
        assert created["status"] == "active"
        goal_id = created["id"]

        res = client.get(f"/api/v1/ledgers/{ledger_id}/goals", headers=_auth(token))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ledger_currency"] == "CNY"
        assert [item["id"] for item in body["items"]] == [goal_id]

        res = client.patch(
            f"/api/v1/ledgers/{ledger_id}/goals/{goal_id}",
            headers=_auth(token),
            json={"saved_amount": 3000.0},
        )
        assert res.status_code == 200, res.text
        patched = res.json()
        # 3000 / 15000 = 20.0%,剩 12000;未提供的字段保持原值
        assert (patched["progress_pct"], patched["remaining_amount"]) == (20.0, 12000.0)
        assert patched["name"] == "Laptop"
        assert patched["priority"] == 10

        res = client.delete(f"/api/v1/ledgers/{ledger_id}/goals/{goal_id}", headers=_auth(token))
        assert res.status_code == 204, res.text

        res = client.get(f"/api/v1/ledgers/{ledger_id}/goals", headers=_auth(token))
        assert res.status_code == 200, res.text
        assert res.json()["items"] == []
    finally:
        app.dependency_overrides.clear()


def test_list_filters_by_status() -> None:
    """?status=active 只回 active 的那条,默认 all 两条都回。"""
    client = _make_client()
    try:
        _register(client, "goals-status@example.com")
        token = _login_web(client, "goals-status@example.com")["access_token"]
        ledger_id = "L_GOALS_STATUS"
        _seed_ledger(client, token, ledger_id)

        active = _create_goal(client, token, ledger_id, name="Active")
        _create_goal(client, token, ledger_id, name="Archived", status="archived")

        res = client.get(
            f"/api/v1/ledgers/{ledger_id}/goals",
            headers=_auth(token),
            params={"status": "active"},
        )
        assert res.status_code == 200, res.text
        assert [item["id"] for item in res.json()["items"]] == [active["id"]]

        res = client.get(f"/api/v1/ledgers/{ledger_id}/goals", headers=_auth(token))
        assert len(res.json()["items"]) == 2
    finally:
        app.dependency_overrides.clear()


def test_create_rejects_non_positive_target_amount() -> None:
    client = _make_client()
    try:
        _register(client, "goals-target@example.com")
        token = _login_web(client, "goals-target@example.com")["access_token"]
        ledger_id = "L_GOALS_TARGET"
        _seed_ledger(client, token, ledger_id)

        res = client.post(
            f"/api/v1/ledgers/{ledger_id}/goals",
            headers=_auth(token),
            json={"name": "Laptop", "target_amount": 0},
        )
        assert res.status_code == 422, res.text
    finally:
        app.dependency_overrides.clear()


def test_create_rejects_past_deadline() -> None:
    client = _make_client()
    try:
        _register(client, "goals-deadline@example.com")
        token = _login_web(client, "goals-deadline@example.com")["access_token"]
        ledger_id = "L_GOALS_DEADLINE"
        _seed_ledger(client, token, ledger_id)

        res = client.post(
            f"/api/v1/ledgers/{ledger_id}/goals",
            headers=_auth(token),
            json={
                "name": "Laptop",
                "target_amount": 15000,
                "deadline": (date.today() - timedelta(days=1)).isoformat(),
            },
        )
        assert res.status_code == 422, res.text
    finally:
        app.dependency_overrides.clear()


def test_create_rejects_blank_name() -> None:
    """``min_length=1`` 拦不住全空白,validator 补这一刀。"""
    client = _make_client()
    try:
        _register(client, "goals-name@example.com")
        token = _login_web(client, "goals-name@example.com")["access_token"]
        ledger_id = "L_GOALS_NAME"
        _seed_ledger(client, token, ledger_id)

        res = client.post(
            f"/api/v1/ledgers/{ledger_id}/goals",
            headers=_auth(token),
            json={"name": "   ", "target_amount": 15000},
        )
        assert res.status_code == 422, res.text
    finally:
        app.dependency_overrides.clear()


def test_patch_rejects_negative_saved_amount() -> None:
    """saved_amount 允许超过 target(超额攒完),但不许负数。"""
    client = _make_client()
    try:
        _register(client, "goals-saved@example.com")
        token = _login_web(client, "goals-saved@example.com")["access_token"]
        ledger_id = "L_GOALS_SAVED"
        _seed_ledger(client, token, ledger_id)
        goal_id = _create_goal(client, token, ledger_id)["id"]

        res = client.patch(
            f"/api/v1/ledgers/{ledger_id}/goals/{goal_id}",
            headers=_auth(token),
            json={"saved_amount": -1},
        )
        assert res.status_code == 422, res.text

        # 超额攒完:saved > target 接受,remaining 夹到 0,progress 可以 > 100
        res = client.patch(
            f"/api/v1/ledgers/{ledger_id}/goals/{goal_id}",
            headers=_auth(token),
            json={"saved_amount": 16500.0, "status": "achieved"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert (body["remaining_amount"], body["progress_pct"]) == (0.0, 110.0)
    finally:
        app.dependency_overrides.clear()


def test_shared_ledger_member_cannot_see_or_touch_others_goals() -> None:
    """共享账本第二层鉴权:B 是同账本 editor,但 A 的目标对 B 一律 404。"""
    client = _make_client()
    try:
        _register(client, "goals-owner@example.com")
        _register(client, "goals-member@example.com")
        owner_token = _login_web(client, "goals-owner@example.com")["access_token"]
        member_token = _login_web(client, "goals-member@example.com")["access_token"]
        ledger_id = "L_GOALS_SHARED"
        _seed_ledger(client, owner_token, ledger_id)
        _share_ledger(
            client,
            owner_token=owner_token,
            member_token=member_token,
            ledger_id=ledger_id,
        )

        goal_id = _create_goal(client, owner_token, ledger_id)["id"]

        # B 能进这个账本(第一层通过),但列表里没有 A 的目标
        res = client.get(f"/api/v1/ledgers/{ledger_id}/goals", headers=_auth(member_token))
        assert res.status_code == 200, res.text
        assert res.json()["items"] == []

        # 指名道姓改 / 删 A 的目标 → 404(不是 403,不泄露存在性)
        res = client.patch(
            f"/api/v1/ledgers/{ledger_id}/goals/{goal_id}",
            headers=_auth(member_token),
            json={"saved_amount": 999.0},
        )
        assert res.status_code == 404, res.text

        res = client.delete(
            f"/api/v1/ledgers/{ledger_id}/goals/{goal_id}",
            headers=_auth(member_token),
        )
        assert res.status_code == 404, res.text

        # A 的目标没被动过
        res = client.get(f"/api/v1/ledgers/{ledger_id}/goals", headers=_auth(owner_token))
        assert [item["saved_amount"] for item in res.json()["items"]] == [0.0]
    finally:
        app.dependency_overrides.clear()


def test_goals_are_not_visible_across_ledgers() -> None:
    """账本维度隔离:A 账本的目标不出现在 B 账本的列表里。"""
    client = _make_client()
    try:
        _register(client, "goals-multi@example.com")
        token = _login_web(client, "goals-multi@example.com")["access_token"]
        _seed_ledger(client, token, "L_GOALS_ONE")
        _seed_ledger(client, token, "L_GOALS_TWO")

        goal_id = _create_goal(client, token, "L_GOALS_ONE")["id"]

        res = client.get("/api/v1/ledgers/L_GOALS_TWO/goals", headers=_auth(token))
        assert res.status_code == 200, res.text
        assert res.json()["items"] == []

        res = client.patch(
            f"/api/v1/ledgers/L_GOALS_TWO/goals/{goal_id}",
            headers=_auth(token),
            json={"saved_amount": 1.0},
        )
        assert res.status_code == 404, res.text
    finally:
        app.dependency_overrides.clear()
