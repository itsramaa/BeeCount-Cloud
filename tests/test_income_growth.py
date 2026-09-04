"""收入增长建议端点契约测试(`POST /api/v1/ai/income-growth`)。

两层,跟实现的两层对齐:

1. 纯函数层(`src/services/income_growth.py`)—— 不起 app、不建表、不造 token,
   直接喂数字:`surplus_ratio` 除零、4 个 lever 分支、4 个 gap_basis 分支 +
   优先级。
2. 端点层 —— boilerplate 沿用 `tests/test_ledger_insights.py` /
   `tests/test_goal_plan.py`:`TestClient` + 内存 SQLite + `dependency_overrides`,
   交易直接写 `read_tx_projection`(读路径唯一权威源),不走 sync/push。

**provider 一律 mock**(monkeypatch 端点命名空间里的 `call_chat_json`),不发真实
网络请求。降级路径同时断言「LLM 没被调用」—— 这个功能的成本控制就在那几个闸门上,
只断言响应体的话闸门失效了测试也不会红。

端点测试把 `routers.ai.income_growth._utcnow` 冻在固定日期上 —— 目标缺口依赖
「从今天到 deadline 还有几个整月」,不冻住就得在测试里把实现的日期算法抄一遍,
那等于用实现验证实现(同 `tests/test_goal_plan.py` 的做法)。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.models import Ledger, ReadTxProjection, UserProfile
from src.routers.ai import income_growth as ig_router
from src.services.ai import ChatProviderError, JsonParseFailedError
from src.services.income_growth import (
    MAX_SUGGESTIONS,
    classify_gap,
    classify_lever,
    surplus_ratio,
)
from src.services.insights import PeriodTotals, SurplusBaseline

_PASSWORD = "123456"
_URL = "/api/v1/ai/income-growth"

# 冻结的「今天」。目标 deadline 全用 15 号,跟它同一个日,整月数因此不受月末夹日
# 影响(见 `services/goal_plan.py::months_remaining` 的 docstring)。
_FROZEN_NOW = datetime(2030, 3, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 纯函数:surplus_ratio
# ---------------------------------------------------------------------------


def test_surplus_ratio_zero_income_does_not_divide_by_zero() -> None:
    """没有收入记录的账本是正常状态,不能抛 ZeroDivisionError。"""
    assert surplus_ratio(0.0, 0.0) == 0.0
    assert surplus_ratio(0.0, 500.0) == 0.0
    assert surplus_ratio(-100.0, 500.0) == 0.0


def test_surplus_ratio_is_a_ratio_not_a_percentage() -> None:
    assert surplus_ratio(5000.0, 200.0) == pytest.approx(0.04)
    assert surplus_ratio(5000.0, 5000.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 纯函数:classify_lever
# ---------------------------------------------------------------------------


def _baseline(
    *,
    median_income: float = 5000.0,
    median_expense: float = 4800.0,
    median_surplus: float = 200.0,
    buffer: float = 0.0,
) -> SurplusBaseline:
    return SurplusBaseline(
        median_income=median_income,
        median_expense=median_expense,
        median_surplus=median_surplus,
        buffer=buffer,
        allocatable=max(0.0, median_surplus - buffer),
        basis_periods=3,
    )


def _current(expense: float, income: float = 5000.0) -> PeriodTotals:
    return PeriodTotals(bucket="2030-03", income=income, expense=expense)


def test_classify_lever_insufficient_data_is_none() -> None:
    """样本不足时不给方向 —— 任何 lever 都是从噪声里读出来的。"""
    assert classify_lever(_baseline(), _current(100.0), 0.04, "insufficient_data") == "none"


def test_classify_lever_thin_surplus_with_normal_spending_is_income() -> None:
    """支出没超基线,结余率却上不去 → 该动收入侧(建议「再省点」是错误建议)。"""
    assert classify_lever(_baseline(), _current(100.0), 0.04, "increase_income") == "income"


def test_classify_lever_overspending_with_healthy_ratio_is_spending() -> None:
    """结余率健康、只是这个周期花超了 → 只动支出侧,不该改成「去挣更多」。"""
    baseline = _baseline(median_expense=1000.0, median_surplus=4000.0)
    # 2000 > 1000 * 1.2 → 超支;ratio 0.8 健康
    assert classify_lever(baseline, _current(2000.0), 0.8, "reduce_spending") == "spending"


def test_classify_lever_overspending_and_thin_surplus_is_both() -> None:
    """只报 reduce_spending 会让用户以为省回基线就够了,而省回基线仍然攒不下钱。"""
    # 6000 > 4800 * 1.2 = 5760 → 超支;ratio 0.04 偏低
    assert classify_lever(_baseline(), _current(6000.0), 0.04, "reduce_spending") == "both"


def test_classify_lever_on_track_is_none() -> None:
    baseline = _baseline(median_expense=1000.0, median_surplus=4000.0)
    assert classify_lever(baseline, _current(1000.0), 0.8, "on_track") == "none"


def test_classify_lever_overspend_boundary_is_exclusive() -> None:
    """正好 1.2 倍不算超支(`>` 不是 `>=`),同 `classify_diagnosis`。"""
    baseline = _baseline(median_expense=1000.0, median_surplus=4000.0)
    assert classify_lever(baseline, _current(1200.0), 0.8, "on_track") == "none"
    assert classify_lever(baseline, _current(1200.01), 0.8, "reduce_spending") == "spending"


# ---------------------------------------------------------------------------
# 纯函数:classify_gap
# ---------------------------------------------------------------------------


def test_classify_gap_goal_shortfall_wins_over_buffer_deficit() -> None:
    """用户自己设的目标缺口盖过服务端推断出来的缓冲缺口 —— 优先级第一条。"""
    baseline = _baseline(median_surplus=200.0, buffer=800.0)  # buffer_deficit 同时成立
    assert classify_gap(baseline, 0.04, 1000.0) == (1000.0, "goal_shortfall")


def test_classify_gap_buffer_deficit_when_surplus_below_own_volatility() -> None:
    """结余撑不住自己的支出波动 = 连自保都做不到,比攒不够钱更急。"""
    baseline = _baseline(median_surplus=200.0, buffer=800.0)
    assert classify_gap(baseline, 0.04, 0.0) == (600.0, "buffer_deficit")


def test_classify_gap_surplus_floor_is_the_weakest_basis() -> None:
    """没有目标、缓冲也够,只是结余率没到 10% 这条通用地板线。"""
    baseline = _baseline(median_income=5000.0, median_surplus=200.0, buffer=0.0)
    gap, basis = classify_gap(baseline, 0.04, 0.0)
    assert (round(gap, 2), basis) == (300.0, "surplus_floor")


def test_classify_gap_none_when_everything_is_fine() -> None:
    baseline = _baseline(median_expense=1000.0, median_surplus=4000.0)
    assert classify_gap(baseline, 0.8, 0.0) == (0.0, "none")


def test_classify_gap_never_returns_negative() -> None:
    """缺口按定义不为负 —— 回负数只会让前端去猜「负缺口」是什么意思。"""
    baseline = _baseline(median_surplus=-500.0, buffer=0.0)
    for shortfall in (0.0, -10.0):
        gap, _basis = classify_gap(baseline, -0.1, shortfall)
        assert gap >= 0.0


def test_classify_gap_float_noise_does_not_flip_the_basis() -> None:
    """半分钱以下的目标缺口是浮点残额,不该把 basis 从 buffer_deficit 翻走。"""
    baseline = _baseline(median_surplus=200.0, buffer=800.0)
    assert classify_gap(baseline, 0.04, 0.001)[1] == "buffer_deficit"


def test_classify_gap_surplus_floor_needs_positive_income() -> None:
    """收入 0 时 `0.10 * 0 - surplus` 不是一个有意义的缺口,落到 none。"""
    baseline = _baseline(median_income=0.0, median_expense=0.0, median_surplus=0.0)
    assert classify_gap(baseline, 0.0, 0.0) == (0.0, "none")


# ---------------------------------------------------------------------------
# 端点 boilerplate
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """限流窗口是模块级全局 dict —— 不清会跨测试串味(11 次那条测试尤其)。"""
    ig_router._RATE_WINDOWS.clear()
    yield
    ig_router._RATE_WINDOWS.clear()


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


def _months_ago(now: datetime, months: int) -> datetime:
    """`months` 个月前那个月的 6 号 12:00 UTC —— 任何月份都落在月内。"""
    total = now.year * 12 + (now.month - 1) - months
    return datetime(total // 12, total % 12 + 1, 6, 12, 0, tzinfo=timezone.utc)


def _seed_months(
    TS: sessionmaker,
    ledger_id: str,
    *,
    incomes: tuple[float, ...],
    expenses: tuple[float, ...],
) -> None:
    """往前 3 个完整月各写一笔收入 + 一笔支出。`incomes[i]` / `expenses[i]` 对应
    「i+1 个月前」。直接写 `read_tx_projection`(读路径唯一权威源),不走 sync/push。
    """
    now = datetime.now(timezone.utc)
    db = TS()
    try:
        ledger = db.scalars(select(Ledger).where(Ledger.external_id == ledger_id)).one()
        for index, (income, expense) in enumerate(zip(incomes, expenses, strict=True)):
            happened = _months_ago(now, index + 1)
            db.add(
                ReadTxProjection(
                    ledger_id=ledger.id,
                    sync_id=f"tx-income-{index}",
                    user_id=ledger.user_id,
                    tx_type="income",
                    amount=income,
                    happened_at=happened,
                    category_name="Salary",
                    created_by_user_id=ledger.user_id,
                )
            )
            db.add(
                ReadTxProjection(
                    ledger_id=ledger.id,
                    sync_id=f"tx-expense-{index}",
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


def _seed_thin_surplus(TS: sessionmaker, ledger_id: str) -> None:
    """收入 5000 ×3,支出 4800 ×3 → median_surplus=200,buffer=0,ratio=0.04。

    → diagnosis=increase_income、lever=income、gap_basis=surplus_floor(300)。
    """
    _seed_months(TS, ledger_id, incomes=(5000.0,) * 3, expenses=(4800.0,) * 3)


def _seed_volatile_thin_surplus(TS: sessionmaker, ledger_id: str) -> None:
    """支出 4000 / 4800 / 5600 → median_expense=4800,buffer=MAD=800,
    逐月结余 1000 / 200 / -600 → median_surplus=200 → buffer_deficit(600)。"""
    _seed_months(TS, ledger_id, incomes=(5000.0,) * 3, expenses=(4000.0, 4800.0, 5600.0))


def _seed_healthy(TS: sessionmaker, ledger_id: str) -> None:
    """收入 5000、支出 1000 → ratio=0.8 → diagnosis=on_track、lever=none、gap=0。"""
    _seed_months(TS, ledger_id, incomes=(5000.0,) * 3, expenses=(1000.0,) * 3)


def _seed_ai_config(TS: sessionmaker, user_id: str) -> None:
    """给用户绑一个 text provider。`apiKey` 故意用可搜索的字面量 —— 有一条测试
    专门断言它不出现在响应体里。"""
    cfg = {
        "providers": [
            {
                "id": "p1",
                "apiKey": "sk-must-never-leak",
                "baseUrl": "https://example.com/v1",
                "textModel": "test-model-x",
            }
        ],
        "binding": {"textProviderId": "p1"},
    }
    db = TS()
    try:
        existing = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
        if existing is not None:
            existing.ai_config_json = json.dumps(cfg)
        else:
            db.add(UserProfile(user_id=user_id, ai_config_json=json.dumps(cfg)))
        db.commit()
    finally:
        db.close()


_CAREER = {
    "occupation": "Backend Engineer",
    "skills": ["python", "sql"],
    "experience_years": 6.0,
    "available_hours_per_week": 8.0,
    "employment_type": "full_time",
    "region": "Shanghai",
}


def _seed_career(client: TestClient, token: str, **overrides) -> None:
    payload = {**_CAREER, **overrides}
    res = client.patch("/api/v1/profile/me", headers=_auth(token), json={"career_profile": payload})
    assert res.status_code == 200, res.text
    assert res.json()["career_profile"] is not None


class _Spy:
    """记录 `call_chat_json` 被调用几次 + 最后一次的 kwargs。"""

    def __init__(self, result: object = None, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result
        self._raises = raises

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._result


def _stub_llm(monkeypatch: pytest.MonkeyPatch, spy: _Spy) -> _Spy:
    monkeypatch.setattr(ig_router, "call_chat_json", spy)
    return spy


def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ig_router, "_utcnow", lambda: _FROZEN_NOW)


def _post(client: TestClient, token: str, ledger_id: str, **overrides) -> dict:
    body = {"ledger_id": ledger_id, "lookback_periods": 4, **overrides}
    res = client.post(_URL, headers=_auth(token), json=body)
    assert res.status_code == 200, res.text
    return res.json()


_ONE_SUGGESTION = {
    "suggestions": [
        {
            "kind": "freelance",
            "title": "接 Python 后端外包",
            "rationale": "你有 6 年后端经验 + 每周 8 小时可投入",
            "monthly_potential_low": 2000,
            "monthly_potential_high": 5000,
            "effort_hours_per_week": 8,
            "time_to_first_income_weeks": 3,
        }
    ]
}


# ---------------------------------------------------------------------------
# 端点:happy path
# ---------------------------------------------------------------------------


def test_income_growth_happy_path_returns_generated_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, TS = _make_client()
    _freeze(monkeypatch)
    spy = _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-happy@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_HAPPY"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)

        body = _post(client, token, ledger_id)

        assert body["generated"] is True
        assert body["generated_reason"] is None
        assert body["model"] == "test-model-x"
        assert body["ledger_id"] == ledger_id
        assert body["currency"] == "CNY"
        assert body["restricted_to_creator"] is False
        assert body["assessment"] == {
            "diagnosis": "increase_income",
            "lever": "income",
            "median_income": 5000.0,
            "median_expense": 4800.0,
            "median_surplus": 200.0,
            "buffer": 0.0,
            "surplus_ratio": 0.04,
            "basis_periods": 3,
            "target_monthly_gap": 300.0,
            "gap_basis": "surplus_floor",
        }
        assert body["suggestions"] == [
            {
                "kind": "freelance",
                "title": "接 Python 后端外包",
                "rationale": "你有 6 年后端经验 + 每周 8 小时可投入",
                "monthly_potential_low": 2000.0,
                "monthly_potential_high": 5000.0,
                "effort_hours_per_week": 8.0,
                "time_to_first_income_weeks": 3.0,
            }
        ]
        assert len(spy.calls) == 1
    finally:
        app.dependency_overrides.clear()


def test_income_growth_passes_cost_controls_to_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout / max_retries / max_tokens 是这个端点唯一的上游成本上界,锁住。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    spy = _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-cost@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_COST"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)

        _post(client, token, ledger_id)

        kwargs = spy.calls[0]
        assert kwargs["timeout"] == 20.0
        assert kwargs["max_retries"] == 0
        assert kwargs["max_tokens"] == 800
    finally:
        app.dependency_overrides.clear()


def test_income_growth_prompt_leaks_no_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """离开服务端的 prompt 里只能有 assessment 的数字 + 职业档案。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    spy = _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-prompt@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_PROMPT"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)

        _post(client, token, ledger_id)

        prompt = json.dumps(spy.calls[0]["messages"], ensure_ascii=False)
        for forbidden in (email, ledger_id, "sk-must-never-leak", "Salary", "Food"):
            assert forbidden not in prompt, f"{forbidden!r} leaked into the prompt"
        # 职业档案确实进去了(否则这个功能没有输入)
        assert "Backend Engineer" in prompt
        # 用户自由文本只进 user 消息,system 消息里没有它
        system = spy.calls[0]["messages"][0]
        assert system["role"] == "system"
        assert "Backend Engineer" not in system["content"]
    finally:
        app.dependency_overrides.clear()


def test_income_growth_response_never_contains_credentials_or_raw_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, TS = _make_client()
    _freeze(monkeypatch)
    _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-noleak@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_NOLEAK"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)

        res = client.post(
            _URL, headers=_auth(token), json={"ledger_id": ledger_id, "lookback_periods": 4}
        )
        assert res.status_code == 200, res.text

        raw = res.text
        for forbidden in ("sk-must-never-leak", "example.com/v1", "Salary", "Food", "tx-income-0"):
            assert forbidden not in raw, f"{forbidden!r} leaked into the response body"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 端点:assessment 的确定性口径
# ---------------------------------------------------------------------------


def test_income_growth_reports_buffer_deficit_when_expenses_are_volatile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结余撑不住自己的支出波动 → buffer_deficit,而不是通用的 surplus_floor。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    _stub_llm(monkeypatch, _Spy(result={"suggestions": []}))
    try:
        email = "ig-buffer@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_BUFFER"
        _seed_ledger(client, token, ledger_id)
        _seed_volatile_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)

        assessment = _post(client, token, ledger_id)["assessment"]

        assert assessment["median_expense"] == 4800.0
        assert assessment["buffer"] == 800.0
        assert assessment["median_surplus"] == 200.0
        assert assessment["lever"] == "income"
        assert (assessment["gap_basis"], assessment["target_monthly_gap"]) == (
            "buffer_deficit",
            600.0,
        )
    finally:
        app.dependency_overrides.clear()


def test_income_growth_goal_shortfall_outranks_buffer_deficit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两条依据同时成立时,用户自己设的目标缺口胜出(优先级第一条)。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    _stub_llm(monkeypatch, _Spy(result={"suggestions": []}))
    try:
        email = "ig-goalgap@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_GOALGAP"
        _seed_ledger(client, token, ledger_id)
        # buffer_deficit(600)成立
        _seed_volatile_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)
        # allocatable = max(0, 200 - 800) = 0 → 目标一分钱也分不到,
        # 缺口 = required_monthly = 12000 / 6 个月 = 2000
        res = client.post(
            f"/api/v1/ledgers/{ledger_id}/goals",
            headers=_auth(token),
            json={
                "name": "Emergency fund",
                "target_amount": 12000.0,
                "deadline": "2030-09-15",
                "priority": 10,
            },
        )
        assert res.status_code == 201, res.text

        assessment = _post(client, token, ledger_id)["assessment"]

        assert (assessment["gap_basis"], assessment["target_monthly_gap"]) == (
            "goal_shortfall",
            2000.0,
        )
    finally:
        app.dependency_overrides.clear()


def test_income_growth_reports_restricted_to_creator_on_shared_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多成员账本上的 median 只统计 caller 自己记的账 —— 必须透出,否则前端会把
    creator 维度的数字当成整个账本的数字展示。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    _stub_llm(monkeypatch, _Spy(result={"suggestions": []}))
    try:
        owner_email = "ig-shared-owner@example.com"
        member_email = "ig-shared-member@example.com"
        owner = _register(client, owner_email)
        _register(client, member_email)
        owner_token = _login_web(client, owner_email)["access_token"]
        member_token = _login_web(client, member_email)["access_token"]
        ledger_id = "L_IG_SHARED"
        _seed_ledger(client, owner_token, ledger_id)
        _share_ledger(
            client, owner_token=owner_token, member_token=member_token, ledger_id=ledger_id
        )
        _seed_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, owner["user"]["id"])
        _seed_career(client, owner_token)

        owner_body = _post(client, owner_token, ledger_id)
        member_body = _post(client, member_token, ledger_id)

        assert owner_body["restricted_to_creator"] is True
        assert owner_body["assessment"]["median_income"] == 5000.0
        # member 一笔没记 → 归属过滤后全 0,不该看到 owner 的数字
        assert member_body["restricted_to_creator"] is True
        assert member_body["assessment"]["median_income"] == 0.0
        assert member_body["generated_reason"] == "insufficient_data"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 端点:5 条降级路径 —— assessment 照常完整,suggestions 为空,LLM 不被调用
# ---------------------------------------------------------------------------

_ASSESSMENT_KEYS = {
    "diagnosis",
    "lever",
    "median_income",
    "median_expense",
    "median_surplus",
    "buffer",
    "surplus_ratio",
    "basis_periods",
    "target_monthly_gap",
    "gap_basis",
}


def _assert_degraded(body: dict, reason: str) -> None:
    """降级契约:200 + reason + 空 suggestions + 无 model,assessment 仍然完整。"""
    assert body["generated"] is False
    assert body["generated_reason"] == reason
    assert body["suggestions"] == []
    assert body["model"] is None
    assert set(body["assessment"]) == _ASSESSMENT_KEYS
    assert all(body["assessment"][k] is not None for k in _ASSESSMENT_KEYS)


def test_income_growth_insufficient_data_skips_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """不足 3 个已完成周期 → 没有可信基线,任何建议都是凭空生成的。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    spy = _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-nodata@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_NODATA"
        _seed_ledger(client, token, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)

        body = _post(client, token, ledger_id)

        _assert_degraded(body, "insufficient_data")
        assert body["assessment"]["diagnosis"] == "insufficient_data"
        assert body["assessment"]["lever"] == "none"
        assert body["assessment"]["basis_periods"] == 3  # 补 0 的空周期
        assert spy.calls == []
    finally:
        app.dependency_overrides.clear()


def test_income_growth_healthy_surplus_skips_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """不为「你没问题」烧一次付费调用。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    spy = _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-healthy@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_HEALTHY"
        _seed_ledger(client, token, ledger_id)
        _seed_healthy(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)

        body = _post(client, token, ledger_id)

        _assert_degraded(body, "lever_not_income")
        assert body["assessment"]["diagnosis"] == "on_track"
        assert body["assessment"]["lever"] == "none"
        assert body["assessment"]["target_monthly_gap"] == 0.0
        assert body["assessment"]["gap_basis"] == "none"
        assert spy.calls == []
    finally:
        app.dependency_overrides.clear()


def test_income_growth_without_career_profile_skips_the_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有技能和可投入时间,LLM 只会吐一份通用清单 —— 这个功能存在的意义正是
    避免通用清单,所以宁可不调用。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    spy = _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-nocareer@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_NOCAREER"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        # 故意不填 career_profile

        body = _post(client, token, ledger_id)

        _assert_degraded(body, "no_career_profile")
        assert body["assessment"]["lever"] == "income"
        assert spy.calls == []
    finally:
        app.dependency_overrides.clear()


def test_income_growth_malformed_career_profile_degrades_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手改过的库行 / 未来版本写进去的形状,不该让用户连结余诊断都拿不到。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    spy = _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-badcareer@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_BADCAREER"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        db = TS()
        try:
            profile = db.scalar(
                select(UserProfile).where(UserProfile.user_id == user["user"]["id"])
            )
            profile.career_profile_json = "{not json at all"
            db.commit()
        finally:
            db.close()

        body = _post(client, token, ledger_id)

        _assert_degraded(body, "no_career_profile")
        assert spy.calls == []
    finally:
        app.dependency_overrides.clear()


def test_income_growth_without_provider_returns_200_not_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有意偏离 `ask.py` 的 400:assessment 才是主要价值,必须扛得住没有 provider。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    spy = _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-noprovider@example.com"
        _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_NOPROVIDER"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        _seed_career(client, token)
        # 故意不 seed ai_config

        body = _post(client, token, ledger_id)

        _assert_degraded(body, "no_provider")
        assert body["assessment"]["median_income"] == 5000.0
        assert body["assessment"]["target_monthly_gap"] == 300.0
        assert spy.calls == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "error",
    [
        ChatProviderError("provider p1 returned 500"),
        JsonParseFailedError("not parseable", raw_content="I'm sorry Dave"),
    ],
    ids=["provider_error", "json_parse_failed"],
)
def test_income_growth_provider_failure_degrades_with_assessment_intact(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """`JsonParseFailedError` 是 `ChatProviderError` 的子类,两者对本端点是同一件事。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    spy = _stub_llm(monkeypatch, _Spy(raises=error))
    try:
        email = f"ig-fail-{type(error).__name__}@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_FAIL"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)

        body = _post(client, token, ledger_id)

        _assert_degraded(body, "provider_failed")
        assert body["assessment"]["median_surplus"] == 200.0
        assert len(spy.calls) == 1  # 确实调过,是上游失败
    finally:
        app.dependency_overrides.clear()


def test_income_growth_career_profile_gate_precedes_provider_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个闸门同时成立时报 no_career_profile —— 先催一个「压根没什么可问」的
    用户去配 AI 服务商是错误引导。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-gateorder@example.com"
        _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_GATEORDER"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        # 既没 ai_config 也没 career_profile

        assert _post(client, token, ledger_id)["generated_reason"] == "no_career_profile"
    finally:
        app.dependency_overrides.clear()


def test_income_growth_data_gate_precedes_career_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有可信基线时连「去填职业档案」都不该催 —— 填了也没有可分析的东西。"""
    client, _TS = _make_client()
    _freeze(monkeypatch)
    _stub_llm(monkeypatch, _Spy(result=_ONE_SUGGESTION))
    try:
        email = "ig-datagate@example.com"
        _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_DATAGATE"
        _seed_ledger(client, token, ledger_id)
        # 空账本 + 没档案 + 没 provider

        assert _post(client, token, ledger_id)["generated_reason"] == "insufficient_data"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 端点:LLM 输出是不可信输入 —— 夹紧不报错
# ---------------------------------------------------------------------------


def _llm_case(
    monkeypatch: pytest.MonkeyPatch, TS: sessionmaker, client: TestClient, result: object
) -> dict:
    email = f"ig-llm-{abs(hash(json.dumps(result, default=str))) % 10**8}@example.com"
    user = _register(client, email)
    token = _login_web(client, email)["access_token"]
    ledger_id = "L_IG_LLM"
    _seed_ledger(client, token, ledger_id)
    _seed_thin_surplus(TS, ledger_id)
    _seed_ai_config(TS, user["user"]["id"])
    _seed_career(client, token)
    _stub_llm(monkeypatch, _Spy(result=result))
    return _post(client, token, ledger_id)


def test_income_growth_unknown_kind_falls_back_to_other(monkeypatch: pytest.MonkeyPatch) -> None:
    client, TS = _make_client()
    _freeze(monkeypatch)
    try:
        body = _llm_case(
            monkeypatch,
            TS,
            client,
            {"suggestions": [{"kind": "buy_bitcoin", "title": "x", "rationale": "y"}]},
        )
        assert body["generated"] is True
        assert body["suggestions"][0]["kind"] == "other"
    finally:
        app.dependency_overrides.clear()


def test_income_growth_drops_entries_without_a_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """title 是这条建议的全部意义,空的就整条丢掉 —— 但不影响其它条。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    try:
        body = _llm_case(
            monkeypatch,
            TS,
            client,
            {
                "suggestions": [
                    {"kind": "freelance", "title": "   ", "rationale": "blank title"},
                    {"kind": "freelance", "rationale": "missing title"},
                    {"kind": "other", "title": "kept", "rationale": "ok"},
                    "not even a dict",
                ]
            },
        )
        assert body["generated"] is True
        assert [s["title"] for s in body["suggestions"]] == ["kept"]
    finally:
        app.dependency_overrides.clear()


def test_income_growth_coerces_bad_numerics_to_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """负数 / 字符串 / bool 一律归 None。`isinstance(True, int)` 是 True,不拦的话
    `"monthly_potential_low": true` 会变成 1.0 元。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    try:
        body = _llm_case(
            monkeypatch,
            TS,
            client,
            {
                "suggestions": [
                    {
                        "kind": "freelance",
                        "title": "t",
                        "rationale": "r",
                        "monthly_potential_low": -500,
                        "monthly_potential_high": "5000 元",
                        "effort_hours_per_week": True,
                        "time_to_first_income_weeks": None,
                    }
                ]
            },
        )
        item = body["suggestions"][0]
        assert item["monthly_potential_low"] is None
        assert item["monthly_potential_high"] is None
        assert item["effort_hours_per_week"] is None
        assert item["time_to_first_income_weeks"] is None
    finally:
        app.dependency_overrides.clear()


def test_income_growth_truncates_overlong_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型不听字数要求是常态,截断而不是 422 —— 否则确定性那半也一起消失。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    try:
        body = _llm_case(
            monkeypatch,
            TS,
            client,
            {"suggestions": [{"kind": "freelance", "title": "T" * 500, "rationale": "R" * 900}]},
        )
        item = body["suggestions"][0]
        assert len(item["title"]) == 120
        assert len(item["rationale"]) == 400
    finally:
        app.dependency_overrides.clear()


def test_income_growth_caps_the_suggestion_count(monkeypatch: pytest.MonkeyPatch) -> None:
    client, TS = _make_client()
    _freeze(monkeypatch)
    try:
        body = _llm_case(
            monkeypatch,
            TS,
            client,
            {
                "suggestions": [
                    {"kind": "freelance", "title": f"idea {i}", "rationale": "r"}
                    for i in range(9)
                ]
            },
        )
        assert len(body["suggestions"]) == MAX_SUGGESTIONS
        assert [s["title"] for s in body["suggestions"]] == [f"idea {i}" for i in range(5)]
    finally:
        app.dependency_overrides.clear()


def test_income_growth_accepts_a_bare_array_from_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """有些模型无视「最外层是 dict」直接给 array。能用就用。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    try:
        body = _llm_case(
            monkeypatch,
            TS,
            client,
            [{"kind": "side_project", "title": "bare array", "rationale": "r"}],
        )
        assert [s["title"] for s in body["suggestions"]] == ["bare array"]
    finally:
        app.dependency_overrides.clear()


def test_income_growth_unusable_payload_stays_generated_with_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 确实答了、只是内容全不可用 —— 前端对这个状态有单独处理,跟「没生成」
    不是一回事,所以 generated 保持 true。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    try:
        body = _llm_case(monkeypatch, TS, client, {"unexpected": "shape"})
        assert body["generated"] is True
        assert body["generated_reason"] is None
        assert body["suggestions"] == []
        assert body["assessment"]["median_income"] == 5000.0
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 端点:限流 / 鉴权 / 入参校验
# ---------------------------------------------------------------------------


def test_income_growth_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """10 次 / 300 秒 / 用户。第 11 次 429 —— 这是唯一挡在付费 API 调用前面的闸。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    _stub_llm(monkeypatch, _Spy(result={"suggestions": []}))
    try:
        email = "ig-ratelimit@example.com"
        user = _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_RATE"
        _seed_ledger(client, token, ledger_id)
        _seed_thin_surplus(TS, ledger_id)
        _seed_ai_config(TS, user["user"]["id"])
        _seed_career(client, token)

        body = {"ledger_id": ledger_id, "lookback_periods": 4}
        for attempt in range(ig_router._RATE_LIMIT_MAX):
            res = client.post(_URL, headers=_auth(token), json=body)
            assert res.status_code == 200, f"call {attempt + 1}: {res.text}"

        res = client.post(_URL, headers=_auth(token), json=body)
        assert res.status_code == 429, res.text
        assert res.json()["error_code"] == "AI_INCOME_GROWTH_RATE_LIMITED"
    finally:
        app.dependency_overrides.clear()


def test_income_growth_rate_limit_is_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个用户打满配额不该拖垮另一个用户。"""
    client, TS = _make_client()
    _freeze(monkeypatch)
    _stub_llm(monkeypatch, _Spy(result={"suggestions": []}))
    try:
        noisy_email = "ig-rate-noisy@example.com"
        quiet_email = "ig-rate-quiet@example.com"
        _register(client, noisy_email)
        _register(client, quiet_email)
        noisy = _login_web(client, noisy_email)["access_token"]
        quiet = _login_web(client, quiet_email)["access_token"]
        noisy_ledger, quiet_ledger = "L_IG_NOISY", "L_IG_QUIET"
        _seed_ledger(client, noisy, noisy_ledger)
        _seed_ledger(client, quiet, quiet_ledger)

        for _ in range(ig_router._RATE_LIMIT_MAX):
            client.post(_URL, headers=_auth(noisy), json={"ledger_id": noisy_ledger})
        assert (
            client.post(_URL, headers=_auth(noisy), json={"ledger_id": noisy_ledger}).status_code
            == 429
        )
        assert (
            client.post(_URL, headers=_auth(quiet), json={"ledger_id": quiet_ledger}).status_code
            == 200
        )
    finally:
        app.dependency_overrides.clear()


def test_income_growth_inaccessible_ledger_returns_404() -> None:
    """非成员一律 404,不是 403 —— 不泄露账本存在性。"""
    client, _TS = _make_client()
    try:
        owner_email = "ig-owner@example.com"
        outsider_email = "ig-outsider@example.com"
        _register(client, owner_email)
        _register(client, outsider_email)
        owner_token = _login_web(client, owner_email)["access_token"]
        outsider_token = _login_web(client, outsider_email)["access_token"]
        ledger_id = "L_IG_PRIVATE"
        _seed_ledger(client, owner_token, ledger_id)

        res = client.post(_URL, headers=_auth(outsider_token), json={"ledger_id": ledger_id})
        assert res.status_code == 404, res.text

        # 压根不存在的账本走同一条路径,响应形状一致(否则 404 也能当探测器用)
        missing = client.post(_URL, headers=_auth(outsider_token), json={"ledger_id": "L_NOPE"})
        assert missing.status_code == 404, missing.text
    finally:
        app.dependency_overrides.clear()


def test_income_growth_requires_authentication() -> None:
    client, _TS = _make_client()
    try:
        res = client.post(_URL, json={"ledger_id": "L_ANY"})
        assert res.status_code == 401, res.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("locale", ["id", "ja", "zh-HK", "", "en-US"])
def test_income_growth_rejects_unsupported_locale(locale: str) -> None:
    """本仓只有 zh / zh-CN / zh-TW / en 四种语言可落地(同 `ask.py:56`),
    别的语种没有对应的 prompt 分支 —— 静默降级成中文比 422 更糟。"""
    client, _TS = _make_client()
    try:
        email = "ig-locale@example.com"
        _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_LOCALE"
        _seed_ledger(client, token, ledger_id)

        res = client.post(
            _URL, headers=_auth(token), json={"ledger_id": ledger_id, "locale": locale}
        )
        assert res.status_code == 422, f"locale={locale!r} → {res.status_code}"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("locale", ["zh", "zh-CN", "zh-TW", "en"])
def test_income_growth_accepts_every_supported_locale(locale: str) -> None:
    client, _TS = _make_client()
    try:
        email = f"ig-locale-ok-{locale}@example.com"
        _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = f"L_IG_LOC_{locale}"
        _seed_ledger(client, token, ledger_id)

        res = client.post(
            _URL, headers=_auth(token), json={"ledger_id": ledger_id, "locale": locale}
        )
        assert res.status_code == 200, res.text
        assert res.json()["generated_reason"] == "insufficient_data"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lookback_periods", 2),
        ("lookback_periods", 25),
        ("tz_offset_minutes", -721),
        ("tz_offset_minutes", 841),
    ],
)
def test_income_growth_rejects_out_of_range_window(field: str, value: int) -> None:
    """窗口范围跟 `/insights` 和 `/goals/plan` 对齐 —— 三个端点吃同一个
    `load_insight_window`,范围不一致会让前端拿到对不上的 median。"""
    client, _TS = _make_client()
    try:
        email = "ig-range@example.com"
        _register(client, email)
        token = _login_web(client, email)["access_token"]
        ledger_id = "L_IG_RANGE"
        _seed_ledger(client, token, ledger_id)

        res = client.post(
            _URL, headers=_auth(token), json={"ledger_id": ledger_id, field: value}
        )
        assert res.status_code == 422, f"{field}={value} → {res.status_code}"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# provider_client 加固:空 choices 不再是 IndexError → 500
# ---------------------------------------------------------------------------


def test_call_chat_json_raises_provider_error_on_empty_choices() -> None:
    """网关返 `{"choices": []}` 时,`.get("choices", [{}])[0]` 抛的 IndexError 会
    冒到 FastAPI 变成 500 INTERNAL_ERROR(外层 try 只 catch httpx 的两个异常)。
    自建网关在「上游全不可用」时确实会返这种 body,所以按 provider 错误如实报。
    """
    import asyncio
    from unittest.mock import patch

    import httpx

    from src.services.ai.provider_client import ChatProviderConfig, call_chat_json

    async def fake_post(self, url, headers=None, json=None, **_):
        return httpx.Response(200, json={"choices": []})

    cfg = ChatProviderConfig(
        provider_id="p1", base_url="https://example.com/v1", api_key="sk-x", model="m"
    )
    with patch("httpx.AsyncClient.post", fake_post):
        with pytest.raises(ChatProviderError) as excinfo:
            asyncio.run(
                call_chat_json(
                    config=cfg,
                    messages=[{"role": "user", "content": "hi"}],
                    max_retries=0,
                )
            )
    assert "no choices" in str(excinfo.value)


def test_call_chat_json_sends_max_tokens_only_when_given() -> None:
    """默认 None → payload 逐字节跟改动前一致(既有调用方零影响)。"""
    import asyncio
    from unittest.mock import patch

    import httpx

    from src.services.ai.provider_client import ChatProviderConfig, call_chat_json

    payloads: list[dict] = []

    async def fake_post(self, url, headers=None, json=None, **_):
        payloads.append(dict(json or {}))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":1}'}}]})

    cfg = ChatProviderConfig(
        provider_id="p1", base_url="https://example.com/v1", api_key="sk-x", model="m"
    )
    msgs = [{"role": "user", "content": "hi"}]
    with patch("httpx.AsyncClient.post", fake_post):
        asyncio.run(call_chat_json(config=cfg, messages=msgs, max_retries=0))
        asyncio.run(call_chat_json(config=cfg, messages=msgs, max_retries=0, max_tokens=800))

    assert "max_tokens" not in payloads[0]
    assert payloads[1]["max_tokens"] == 800
