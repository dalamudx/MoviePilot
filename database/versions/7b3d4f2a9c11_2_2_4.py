from alembic import op
import sqlalchemy as sa


revision = "7b3d4f2a9c11"
down_revision = "58edfac72c32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    dh_columns = inspector.get_columns("downloadhistory")
    if not any(c["name"] == "subscribe_id" for c in dh_columns):
        op.add_column(
            "downloadhistory", sa.Column("subscribe_id", sa.Integer(), nullable=True)
        )
    dh_indexes = inspector.get_indexes("downloadhistory")
    if not any(i["name"] == "ix_downloadhistory_subscribe_id" for i in dh_indexes):
        op.create_index(
            "ix_downloadhistory_subscribe_id",
            "downloadhistory",
            ["subscribe_id"],
            unique=False,
        )

    th_columns = inspector.get_columns("transferhistory")
    if not any(c["name"] == "subscribe_id" for c in th_columns):
        op.add_column(
            "transferhistory", sa.Column("subscribe_id", sa.Integer(), nullable=True)
        )
    th_indexes = inspector.get_indexes("transferhistory")
    if not any(i["name"] == "ix_transferhistory_subscribe_id" for i in th_indexes):
        op.create_index(
            "ix_transferhistory_subscribe_id",
            "transferhistory",
            ["subscribe_id"],
            unique=False,
        )

    tables = inspector.get_table_names()
    if "subscribeprogress" not in tables:
        op.create_table(
            "subscribeprogress",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("subscribe_id", sa.Integer(), nullable=False),
            sa.Column("tmdbid", sa.Integer(), nullable=True),
            sa.Column("doubanid", sa.String(), nullable=True),
            sa.Column("media_type", sa.String(), nullable=True),
            sa.Column("season", sa.Integer(), nullable=True),
            sa.Column("total_episode", sa.Integer(), nullable=True),
            sa.Column("pending_episodes", sa.JSON(), nullable=True),
            sa.Column("downloaded_episodes", sa.JSON(), nullable=True),
            sa.Column("transferred_episodes", sa.JSON(), nullable=True),
            sa.Column("completed", sa.Boolean(), nullable=True),
            sa.Column("last_download_hash", sa.String(), nullable=True),
            sa.Column("date", sa.String(), nullable=True),
            sa.Column("last_update", sa.String(), nullable=True),
        )
        op.create_index(
            "ix_subscribeprogress_subscribe_id",
            "subscribeprogress",
            ["subscribe_id"],
            unique=True,
        )
        op.create_index(
            "ix_subscribeprogress_tmdbid", "subscribeprogress", ["tmdbid"], unique=False
        )
        op.create_index(
            "ix_subscribeprogress_doubanid",
            "subscribeprogress",
            ["doubanid"],
            unique=False,
        )
        op.create_index(
            "ix_subscribeprogress_completed",
            "subscribeprogress",
            ["completed"],
            unique=False,
        )
        op.create_index(
            "ix_subscribeprogress_last_download_hash",
            "subscribeprogress",
            ["last_download_hash"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    if "subscribeprogress" in tables:
        op.drop_index(
            "ix_subscribeprogress_last_download_hash", table_name="subscribeprogress"
        )
        op.drop_index("ix_subscribeprogress_completed", table_name="subscribeprogress")
        op.drop_index("ix_subscribeprogress_doubanid", table_name="subscribeprogress")
        op.drop_index("ix_subscribeprogress_tmdbid", table_name="subscribeprogress")
        op.drop_index(
            "ix_subscribeprogress_subscribe_id", table_name="subscribeprogress"
        )
        op.drop_table("subscribeprogress")

    th_columns = [c["name"] for c in inspector.get_columns("transferhistory")]
    if "subscribe_id" in th_columns:
        for index in inspector.get_indexes("transferhistory"):
            if index["name"] == "ix_transferhistory_subscribe_id":
                op.drop_index(
                    "ix_transferhistory_subscribe_id", table_name="transferhistory"
                )
                break
        op.drop_column("transferhistory", "subscribe_id")

    dh_columns = [c["name"] for c in inspector.get_columns("downloadhistory")]
    if "subscribe_id" in dh_columns:
        for index in inspector.get_indexes("downloadhistory"):
            if index["name"] == "ix_downloadhistory_subscribe_id":
                op.drop_index(
                    "ix_downloadhistory_subscribe_id", table_name="downloadhistory"
                )
                break
        op.drop_column("downloadhistory", "subscribe_id")
