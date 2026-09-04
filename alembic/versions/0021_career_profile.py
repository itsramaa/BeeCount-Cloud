"""career_profile — UserProfile 上的职业档案 JSON blob(server-only,不参与同步)

Revision ID: 0021_career_profile
Revises: 0020_financial_goals
Create Date: 2026-09-04

user_profiles 加一列 career_profile_json(Text,nullable):occupation /
skills / experience_years / available_hours_per_week / employment_type /
region / notes。为将来的"收入增长建议"AI 端点做持久化底座。

为什么单开一列而不是并租进 appearance_json / ai_config_json:那两个 blob 会被
mobile push **整体替换**,寄放在里面的额外 key 会被静默清掉。这一列 server
独占,当前没有 mobile 写入方。

另外这个 blob 要进 LLM prompt,所以跟 mobile 自治的那两个 blob 不同,写入前在
server 端过 schema 校验(schemas.CareerProfile),不透传。读取时校验失败返
None 而不是 500 —— 一个可选 blob 不该让用户读不出 profile。

**不是 sync 实体**:没有配套 read_*_projection、不写 sync_changes、没在
sync_applier 的 _MERGE_SPECS / _UPSERT_DISPATCH / _DELETE_DISPATCH 登记。
数据只经 src/routers/profile.py 的 GET / PATCH /profile/me 进出。
"""

import sqlalchemy as sa
from alembic import op


revision = "0021_career_profile"
down_revision = "0020_financial_goals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_profiles",
        sa.Column("career_profile_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_profiles", "career_profile_json")
