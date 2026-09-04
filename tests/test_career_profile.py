"""career_profile 往返契约测试(UserProfile 上的 server-only JSON blob)。

覆盖:
1. 全字段往返 + 只传一个字段的部分写入(未填字段不落 null)
2. ``None`` = 不动这个字段 —— 改 display_name 不该清掉已存的 career_profile
3. ``{}`` 清空到 NULL
4. 越界值 / 非法枚举 / 超量 skills → 422
5. ``extra="ignore"``:未知 key 不 422,也不回显
6. 全空白字符串归一成"没填"
7. 库里存了坏 JSON 时 GET /profile/me 返 200 + null,不是 500
8. career_profile 的写入不碰 appearance / ai_config

boilerplate 沿用 tests/test_goals_crud.py 的 _make_client / _register。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.models import UserProfile

_PASSWORD = "123456"
_PROFILE_URL = "/api/v1/profile/me"

_FULL_CAREER = {
    "occupation": "Backend Engineer",
    "skills": ["python", "sql", "fastapi"],
    "experience_years": 6.5,
    "available_hours_per_week": 10.0,
    "employment_type": "full_time",
    "region": "Shanghai",
    "notes": "wants a side income stream",
}


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


def _auth_header(client: TestClient, email: str) -> dict[str, str]:
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
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _patch(client: TestClient, hdr: dict[str, str], body: dict):
    return client.patch(_PROFILE_URL, headers=hdr, json=body)


def _get_career(client: TestClient, hdr: dict[str, str]) -> dict | None:
    res = client.get(_PROFILE_URL, headers=hdr)
    assert res.status_code == 200, res.text
    return res.json()["career_profile"]


def _stored_blob(TS: sessionmaker) -> str | None:
    with TS() as db:
        profile = db.scalar(select(UserProfile))
        assert profile is not None
        return profile.career_profile_json


def test_career_profile_full_roundtrip():
    client, _ = _make_client()
    try:
        hdr = _auth_header(client, "cp-full@t.com")
        res = _patch(client, hdr, {"career_profile": _FULL_CAREER})
        assert res.status_code == 200, res.text
        assert res.json()["career_profile"] == _FULL_CAREER
        assert _get_career(client, hdr) == _FULL_CAREER
    finally:
        app.dependency_overrides.clear()


def test_career_profile_partial_write_leaves_unset_fields_absent():
    """只填 occupation 时,其余 key 不该以显式 null 落库。"""
    client, TS = _make_client()
    try:
        hdr = _auth_header(client, "cp-partial@t.com")
        assert _patch(client, hdr, {"career_profile": {"occupation": "Teacher"}}).status_code == 200
        assert json.loads(_stored_blob(TS) or "{}") == {"occupation": "Teacher"}
        body = _get_career(client, hdr)
        assert body is not None
        assert body["occupation"] == "Teacher"
        assert all(body[key] is None for key in body if key != "occupation")
    finally:
        app.dependency_overrides.clear()


def test_career_profile_survives_unrelated_patch():
    """None = 不动。改 display_name 不该顺手清掉 career_profile。"""
    client, _ = _make_client()
    try:
        hdr = _auth_header(client, "cp-untouched@t.com")
        _patch(client, hdr, {"career_profile": _FULL_CAREER})
        assert _patch(client, hdr, {"display_name": "nick"}).status_code == 200
        assert _get_career(client, hdr) == _FULL_CAREER
    finally:
        app.dependency_overrides.clear()


def test_career_profile_empty_dict_clears_column():
    client, TS = _make_client()
    try:
        hdr = _auth_header(client, "cp-clear@t.com")
        _patch(client, hdr, {"career_profile": _FULL_CAREER})
        assert _patch(client, hdr, {"career_profile": {}}).status_code == 200
        assert _stored_blob(TS) is None
        assert _get_career(client, hdr) is None
    finally:
        app.dependency_overrides.clear()


def test_career_profile_invalid_values_rejected():
    client, _ = _make_client()
    try:
        hdr = _auth_header(client, "cp-invalid@t.com")
        rejected = [
            {"available_hours_per_week": 200},
            {"experience_years": -1},
            {"skills": [f"s{i}" for i in range(21)]},
            {"skills": ["x" * 65]},
            # 非 str 的项不该被静默丢掉,要报类型错
            {"skills": [123]},
            {"employment_type": "astronaut"},
        ]
        for payload in rejected:
            res = _patch(client, hdr, {"career_profile": payload})
            assert res.status_code == 422, (payload, res.status_code, res.text)
    finally:
        app.dependency_overrides.clear()


def test_career_profile_unknown_key_ignored_not_rejected():
    """extra="ignore":未来客户端推来的陌生 key 不该 422 掉整个 PATCH。"""
    client, _ = _make_client()
    try:
        hdr = _auth_header(client, "cp-extra@t.com")
        res = _patch(
            client,
            hdr,
            {"career_profile": {"occupation": "Chef", "future_field": "whatever"}},
        )
        assert res.status_code == 200, res.text
        assert "future_field" not in res.json()["career_profile"]
        assert res.json()["career_profile"]["occupation"] == "Chef"
    finally:
        app.dependency_overrides.clear()


def test_career_profile_whitespace_only_normalizes_to_none():
    client, TS = _make_client()
    try:
        hdr = _auth_header(client, "cp-blank@t.com")
        res = _patch(client, hdr, {"career_profile": {"occupation": "   ", "skills": ["", "  "]}})
        assert res.status_code == 200, res.text
        assert res.json()["career_profile"] is None
        # 全部字段归一成 None → model_dump(exclude_none=True) 空 dict → 存 NULL
        assert _stored_blob(TS) is None
    finally:
        app.dependency_overrides.clear()


def test_career_profile_malformed_db_row_reads_as_null():
    """手改过的库行不该让用户读不出 profile。"""
    client, TS = _make_client()
    try:
        hdr = _auth_header(client, "cp-corrupt@t.com")
        _patch(client, hdr, {"career_profile": _FULL_CAREER})
        with TS() as db:
            profile = db.scalar(select(UserProfile))
            assert profile is not None
            profile.career_profile_json = '{"experience_years": "not a number"}'
            db.commit()
        res = client.get(_PROFILE_URL, headers=hdr)
        assert res.status_code == 200, res.text
        assert res.json()["career_profile"] is None
    finally:
        app.dependency_overrides.clear()


def test_career_profile_patch_does_not_clobber_other_blobs():
    client, _ = _make_client()
    try:
        hdr = _auth_header(client, "cp-blobs@t.com")
        appearance = {"compact_amount": True}
        ai_config = {"strategy": "cloud_first"}
        _patch(client, hdr, {"appearance": appearance, "ai_config": ai_config})
        assert _patch(client, hdr, {"career_profile": _FULL_CAREER}).status_code == 200
        body = client.get(_PROFILE_URL, headers=hdr).json()
        assert body["appearance"] == appearance
        assert body["ai_config"] == ai_config
        assert body["career_profile"] == _FULL_CAREER
    finally:
        app.dependency_overrides.clear()
