"""financial_goals — 攒钱目标(server-only,不参与同步)

Revision ID: 0020_financial_goals
Revises: 0019_account_hidden
Create Date: 2026-09-03

新建 financial_goals 表:
  - user_id / ledger_id:两个 CASCADE FK。ledger_id **故意 NOT NULL** ——
    结余是账本维度算出来的量,跨账本分配没有可辩护的语义;跟 budget 同口径。
  - name / target_amount:目标名 + 目标金额(API 层限 target_amount > 0)
  - saved_amount:已攒金额,手动 PATCH 推进,server_default "0"
  - currency:创建时 Ledger.currency 的快照,账本后来改币种不回写既有目标
  - deadline:可空 Date。创建时要求在未来(API 层校验),DB 不约束
  - priority:分配权重,越大越优先,API 层限 0-100,server_default "0"
  - status:'active' | 'achieved' | 'archived',server_default "active"

复合索引 ix_financial_goals_user_ledger:`(user_id, ledger_id)` 命中主查询
形态「我在这个账本下的目标列表」。读写两条路径都强制叠 user_id 过滤 ——
共享账本里成员之间的目标互不可见。

**不是 sync 实体**:没有配套 read_*_projection、不写 sync_changes、没在
sync_applier 的 _MERGE_SPECS / _UPSERT_DISPATCH / _DELETE_DISPATCH 登记。
数据只经 src/routers/goals.py 的 CRUD 进出,mobile 端当前看不到。
"""

import sqlalchemy as sa
from alembic import op


revision = "0020_financial_goals"
down_revision = "0019_account_hidden"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_goals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "ledger_id",
            sa.String(36),
            sa.ForeignKey("ledgers.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("target_amount", sa.Float(), nullable=False),
        sa.Column("saved_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_financial_goals_user_ledger",
        "financial_goals",
        ["user_id", "ledger_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_financial_goals_user_ledger", table_name="financial_goals")
    op.drop_table("financial_goals")
