import re

from alembic import op
import sqlalchemy as sa


MOVIE_TYPE = "电影"


revision = "9c1f0b2d6e44"
down_revision = "7b3d4f2a9c11"
branch_labels = None
depends_on = None


def _parse_seasons(season_str: str | None) -> list[int]:
    if not season_str:
        return []
    try:
        season_str = season_str.upper().replace("S", "").strip()
        if "-" in season_str:
            parts = season_str.split("-")
            start = int(parts[0])
            end = int(parts[-1])
            if start > end:
                start, end = end, start
            return list(range(start, end + 1))
        return [int(season_str)]
    except Exception:
        return []


def _parse_episodes(episode_str: str | None) -> list[int]:
    if not episode_str:
        return []
    try:
        episode_str = episode_str.upper().replace("EP", "E").strip()
        nums = [int(n) for n in re.findall(r"\d{1,4}", episode_str)]
        if not nums:
            return []
        if "-" in episode_str and len(nums) >= 2:
            start = nums[0]
            end = nums[1]
            if start > end:
                start, end = end, start
            return list(range(start, end + 1))
        return sorted(list(set(nums)))
    except Exception:
        return []


def upgrade() -> None:
    conn = op.get_bind()
    metadata = sa.MetaData()
    subscribe = sa.Table("subscribe", metadata, autoload_with=conn)
    subscribeprogress = sa.Table("subscribeprogress", metadata, autoload_with=conn)
    transferhistory = sa.Table("transferhistory", metadata, autoload_with=conn)

    existing_progress_ids = {
        row[0]
        for row in conn.execute(sa.select(subscribeprogress.c.subscribe_id)).fetchall()
        if row[0] is not None
    }

    subscribe_rows = conn.execute(sa.select(subscribe)).mappings().all()
    for sub in subscribe_rows:
        subscribe_id = sub.get("id")
        if not subscribe_id or subscribe_id in existing_progress_ids:
            continue

        transferred: set[int] = set()
        histories = (
            conn.execute(
                sa.select(transferhistory).where(transferhistory.c.status == True)
            )
            .mappings()
            .all()
        )

        for history in histories:
            history_subscribe_id = history.get("subscribe_id")
            matched = False

            if history_subscribe_id:
                matched = history_subscribe_id == subscribe_id
            elif sub.get("tmdbid") and history.get("tmdbid"):
                matched = history.get("tmdbid") == sub.get("tmdbid")
            elif sub.get("doubanid") and history.get("doubanid"):
                matched = history.get("doubanid") == sub.get("doubanid")
            elif sub.get("name") and sub.get("year"):
                matched = history.get("title") == sub.get("name") and history.get(
                    "year"
                ) == sub.get("year")

            if not matched:
                continue

            if sub.get("type") == MOVIE_TYPE:
                transferred.add(1)
                continue

            seasons = _parse_seasons(history.get("seasons"))
            if sub.get("season") and seasons and sub.get("season") not in seasons:
                continue

            parsed_episodes = _parse_episodes(history.get("episodes"))
            if parsed_episodes:
                transferred.update(parsed_episodes)
            elif sub.get("total_episode"):
                start_episode = sub.get("start_episode") or 1
                total_episode = sub.get("total_episode") or 0
                transferred.update(range(start_episode, start_episode + total_episode))

        total_episode = sub.get("total_episode")
        transferred_list = sorted(list(transferred))
        completed = False
        if sub.get("type") == MOVIE_TYPE:
            completed = bool(transferred_list)
        elif total_episode:
            completed = len(transferred_list) >= total_episode

        conn.execute(
            subscribeprogress.insert().values(
                subscribe_id=subscribe_id,
                tmdbid=sub.get("tmdbid"),
                doubanid=sub.get("doubanid"),
                media_type=sub.get("type"),
                season=sub.get("season"),
                total_episode=total_episode,
                pending_episodes=[],
                downloaded_episodes=transferred_list,
                transferred_episodes=transferred_list,
                completed=completed,
                last_download_hash=None,
                date=sub.get("date"),
                last_update=sub.get("last_update") or sub.get("date"),
            )
        )


def downgrade() -> None:
    pass
