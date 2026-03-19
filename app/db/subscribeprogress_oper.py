import time
from typing import Iterable, List, Optional

from app.db import DbOper
from app.db.models.subscribe import Subscribe
from app.db.models.subscribeprogress import SubscribeProgress
from app.schemas.types import MediaType


class SubscribeProgressOper(DbOper):
    @staticmethod
    def _normalize_episodes(episodes: Optional[Iterable[int]]) -> List[int]:
        if not episodes:
            return []
        normalized = []
        for episode in episodes:
            try:
                normalized.append(int(episode))
            except (TypeError, ValueError):
                continue
        return sorted(list(set(normalized)))

    @staticmethod
    def _merge_episodes(*episode_groups: Optional[Iterable[int]]) -> List[int]:
        merged = set()
        for episodes in episode_groups:
            merged.update(SubscribeProgressOper._normalize_episodes(episodes))
        return sorted(list(merged))

    @staticmethod
    def _get_target_episodes(
        subscribe: Subscribe, total_episode: Optional[int]
    ) -> List[int]:
        if subscribe.type == MediaType.MOVIE.value:
            return [1]
        if not total_episode:
            return []
        start_episode = subscribe.start_episode or 1
        if total_episode < start_episode:
            return []
        return list(range(start_episode, total_episode + 1))

    @staticmethod
    def calc_target_episode_count(
        start_episode: Optional[int],
        total_episode: Optional[int],
        media_type: Optional[str],
    ) -> Optional[int]:
        if media_type == MediaType.MOVIE.value:
            return 1
        if not total_episode:
            return None
        start = start_episode or 1
        if total_episode < start:
            return 0
        return total_episode - start + 1

    def get_by_subscribe_id(self, subscribe_id: int) -> Optional[SubscribeProgress]:
        return SubscribeProgress.get_by_subscribe_id(self._db, subscribe_id)

    async def async_get_by_subscribe_id(
        self, subscribe_id: int
    ) -> Optional[SubscribeProgress]:
        return await SubscribeProgress.async_get_by_subscribe_id(self._db, subscribe_id)

    async def async_create_for_subscribe(
        self, subscribe: Subscribe
    ) -> Optional[SubscribeProgress]:
        if not subscribe:
            return None
        progress = await self.async_get_by_subscribe_id(subscribe.id)
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        payload = {
            "tmdbid": subscribe.tmdbid,
            "doubanid": subscribe.doubanid,
            "media_type": subscribe.type,
            "season": subscribe.season,
            "total_episode": subscribe.total_episode,
            "last_update": now,
        }
        if progress:
            await progress.async_update(self._db, payload)
            return progress

        progress = SubscribeProgress(
            **{
                "subscribe_id": subscribe.id,
                "pending_episodes": [],
                "downloaded_episodes": [],
                "transferred_episodes": [],
                "completed": False,
                "date": now,
                **payload,
            }
        )
        await progress.async_create(self._db)
        return await self.async_get_by_subscribe_id(subscribe.id)

    def create_for_subscribe(self, subscribe: Subscribe) -> Optional[SubscribeProgress]:
        if not subscribe:
            return None
        progress = self.get_by_subscribe_id(subscribe.id)
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        payload = {
            "tmdbid": subscribe.tmdbid,
            "doubanid": subscribe.doubanid,
            "media_type": subscribe.type,
            "season": subscribe.season,
            "total_episode": subscribe.total_episode,
            "last_update": now,
        }
        if progress:
            progress.update(self._db, payload)
            return progress

        progress = SubscribeProgress(
            **{
                "subscribe_id": subscribe.id,
                "pending_episodes": [],
                "downloaded_episodes": [],
                "transferred_episodes": [],
                "completed": False,
                "date": now,
                **payload,
            }
        )
        progress.create(self._db)
        return self.get_by_subscribe_id(subscribe.id)

    def ensure_for_subscribe(self, subscribe: Subscribe) -> Optional[SubscribeProgress]:
        if not subscribe:
            return None
        return self.get_by_subscribe_id(subscribe.id) or self.create_for_subscribe(
            subscribe
        )

    def update_total_episode(
        self, subscribe: Subscribe, total_episode: Optional[int]
    ) -> Optional[SubscribeProgress]:
        progress = self.ensure_for_subscribe(subscribe)
        if not progress:
            return None
        progress.update(
            self._db,
            {
                "total_episode": total_episode,
                "last_update": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            },
        )
        return self.get_by_subscribe_id(subscribe.id)

    def mark_pending(
        self,
        subscribe: Subscribe,
        episodes: Optional[Iterable[int]],
        download_hash: Optional[str] = None,
    ) -> Optional[SubscribeProgress]:
        progress = self.ensure_for_subscribe(subscribe)
        if not progress:
            return None
        normalized = self._normalize_episodes(episodes)
        if not normalized and subscribe.type == MediaType.MOVIE.value:
            normalized = [1]
        pending = self._merge_episodes(progress.pending_episodes, normalized)
        progress.update(
            self._db,
            {
                "pending_episodes": pending,
                "last_download_hash": download_hash,
                "last_update": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            },
        )
        return self.get_by_subscribe_id(subscribe.id)

    def mark_transferred(
        self,
        subscribe: Subscribe,
        episodes: Optional[Iterable[int]],
        download_hash: Optional[str] = None,
    ) -> Optional[SubscribeProgress]:
        progress = self.ensure_for_subscribe(subscribe)
        if not progress:
            return None

        normalized = self._normalize_episodes(episodes)
        if not normalized and subscribe.type == MediaType.MOVIE.value:
            normalized = [1]

        transferred = self._merge_episodes(progress.transferred_episodes, normalized)
        downloaded = self._merge_episodes(progress.downloaded_episodes, normalized)
        pending = sorted(
            list(
                set(self._normalize_episodes(progress.pending_episodes)).difference(
                    set(normalized)
                )
            )
        )
        total_episode = progress.total_episode or subscribe.total_episode
        target_episodes = self._get_target_episodes(subscribe, total_episode)

        completed = False
        if subscribe.type == MediaType.MOVIE.value:
            completed = bool(transferred)
        elif target_episodes:
            completed = set(target_episodes).issubset(set(transferred))

        progress.update(
            self._db,
            {
                "pending_episodes": pending,
                "downloaded_episodes": downloaded,
                "transferred_episodes": transferred,
                "completed": completed,
                "last_download_hash": download_hash,
                "last_update": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            },
        )
        return self.get_by_subscribe_id(subscribe.id)

    def calc_lack_episode(self, subscribe: Subscribe) -> Optional[int]:
        progress = self.get_by_subscribe_id(subscribe.id)
        if not progress or subscribe.type != MediaType.TV.value:
            return None
        total_episode = progress.total_episode or subscribe.total_episode
        if not total_episode:
            return None
        target_episodes = self._get_target_episodes(subscribe, total_episode)
        if not target_episodes:
            return None
        transferred = set(self._normalize_episodes(progress.transferred_episodes))
        return len(
            [episode for episode in target_episodes if episode not in transferred]
        )

    def is_complete(self, subscribe: Subscribe) -> bool:
        progress = self.get_by_subscribe_id(subscribe.id)
        if not progress:
            return False
        if progress.completed:
            return True
        if subscribe.type == MediaType.MOVIE.value:
            return bool(self._normalize_episodes(progress.transferred_episodes))
        total_episode = progress.total_episode or subscribe.total_episode
        target_episodes = self._get_target_episodes(subscribe, total_episode)
        if not target_episodes:
            return False
        transferred = set(self._normalize_episodes(progress.transferred_episodes))
        return set(target_episodes).issubset(transferred)

    def get_transferred_episodes(self, subscribe: Subscribe) -> List[int]:
        progress = self.get_by_subscribe_id(subscribe.id)
        if not progress:
            return []
        transferred = self._normalize_episodes(progress.transferred_episodes)
        if transferred:
            return transferred
        if subscribe.type == MediaType.MOVIE.value and progress.completed:
            return [1]
        return []

    def get_pending_episodes(self, subscribe: Subscribe) -> List[int]:
        progress = self.get_by_subscribe_id(subscribe.id)
        if not progress:
            return []
        return self._normalize_episodes(progress.pending_episodes)

    def rollback_by_hash(
        self,
        subscribe: Subscribe,
        download_hash: str,
        episodes: Optional[Iterable[int]],
    ) -> Optional[SubscribeProgress]:
        progress = self.ensure_for_subscribe(subscribe)
        if not progress:
            return None

        normalized = self._normalize_episodes(episodes)
        if not normalized and subscribe.type == MediaType.MOVIE.value:
            normalized = [1]

        pending = sorted(
            list(
                set(self._normalize_episodes(progress.pending_episodes)).difference(
                    set(normalized)
                )
            )
        )
        downloaded = sorted(
            list(
                set(self._normalize_episodes(progress.downloaded_episodes)).difference(
                    set(normalized)
                )
            )
        )
        transferred = sorted(
            list(
                set(self._normalize_episodes(progress.transferred_episodes)).difference(
                    set(normalized)
                )
            )
        )
        total_episode = progress.total_episode or subscribe.total_episode
        target_episodes = self._get_target_episodes(subscribe, total_episode)

        completed = False
        if subscribe.type == MediaType.MOVIE.value:
            completed = bool(transferred)
        elif target_episodes:
            completed = set(target_episodes).issubset(set(transferred))

        last_download_hash = progress.last_download_hash
        if download_hash and last_download_hash == download_hash:
            last_download_hash = None

        progress.update(
            self._db,
            {
                "pending_episodes": pending,
                "downloaded_episodes": downloaded,
                "transferred_episodes": transferred,
                "completed": completed,
                "last_download_hash": last_download_hash,
                "last_update": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            },
        )
        return self.get_by_subscribe_id(subscribe.id)

    def delete_by_subscribe_id(self, subscribe_id: int):
        progress = self.get_by_subscribe_id(subscribe_id)
        if progress:
            progress.delete(self._db, progress.id)
