from enum import Enum
from typing import Dict, List, Optional, Set
from pathlib import Path
import time
import json
import threading

from app.core.cache import TTLCache, Cache, FileCache
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.utils.singleton import SingletonClass
from app.log import logger


class DownloadState(str, Enum):
    """下载状态枚举"""
    DOWNLOADING = "downloading"     # 下载中
    COMPLETED = "completed"        # 下载完成，待整理
    TRANSFERRED = "transferred"    # 已整理完成
    DELETED = "deleted"           # 已删除


class DownloadStateManager(metaclass=SingletonClass):
    """下载状态管理器"""
    
    def __init__(self):
        # 短期状态缓存，1小时TTL，用于快速查询
        self._state_cache = TTLCache(region="download_state", maxsize=10000, ttl=3600)
        
        # 长期状态持久化，使用FileCache，重启后仍然有效
        self._persistent_cache = FileCache(base=settings.CACHE_PATH / "download_states")
        
        # 待检出缓存，24小时TTL
        self._pending_cache = Cache(cache_type='ttl', maxsize=5000, ttl=24*3600)
        
        # 活跃下载Hash集合，用于快速判断
        self._active_downloads: Set[str] = set()
        
        # 线程锁
        self._lock = threading.RLock()
        
        # 启动时恢复持久化状态
        self._restore_persistent_states()
    
    def _restore_persistent_states(self):
        """启动时恢复持久化状态"""
        try:
            # 从持久化存储中恢复活跃下载列表
            active_data = self._persistent_cache.get("active_downloads")
            if active_data:
                restored_hashes = json.loads(active_data.decode())
                with self._lock:
                    self._active_downloads.update(restored_hashes)
                logger.info(f"恢复了 {len(restored_hashes)} 个活跃下载状态")
        except Exception as e:
            logger.error(f"恢复持久化状态失败: {e}")
    
    def mark_downloading(self, hash_str: str, mediainfo: MediaInfo, 
                        meta: MetaBase, episodes: List[int] = None):
        """标记为下载中，支持长期存储"""
        if not hash_str or not mediainfo:
            return
            
        state_data = {
            "hash": hash_str,
            "state": DownloadState.DOWNLOADING.value,
            "tmdbid": mediainfo.tmdb_id,
            "doubanid": mediainfo.douban_id,
            "title": mediainfo.title,
            "year": mediainfo.year,
            "type": mediainfo.type.value,
            "season": meta.season if meta else None,
            "episodes": episodes or [],
            "start_time": time.time(),
            "update_time": time.time(),
            "last_check": time.time()
        }
        
        with self._lock:
            # 存储到短期缓存
            self._state_cache.set(hash_str, state_data)
            
            # 持久化存储关键信息
            self._persistent_cache.set(f"state_{hash_str}", 
                                      json.dumps(state_data).encode())
            
            # 添加到活跃下载集合
            self._active_downloads.add(hash_str)
            self._save_active_downloads()
        
        # 更新待检出缓存
        self._update_pending_cache(mediainfo, meta, episodes, "add")
        
        logger.info(f"标记下载中: {mediainfo.title_year} - {hash_str}")
    
    def mark_transferred(self, hash_str: str):
        """标记为已整理完成"""
        if not hash_str:
            return
            
        state_data = self._get_state_data(hash_str)
        if state_data:
            state_data["state"] = DownloadState.TRANSFERRED.value
            state_data["update_time"] = time.time()
            state_data["transfer_time"] = time.time()
            
            with self._lock:
                # 更新缓存
                self._state_cache.set(hash_str, state_data)
                self._persistent_cache.set(f"state_{hash_str}", 
                                          json.dumps(state_data).encode())
                
                # 从活跃下载中移除
                self._active_downloads.discard(hash_str)
                self._save_active_downloads()
            
            # 从待检出缓存中移除
            self._remove_from_pending(state_data)
            
            logger.info(f"标记已整理: {state_data.get('title')} - {hash_str}")
    
    def mark_deleted(self, hash_str: str):
        """标记为已删除"""
        if not hash_str:
            return None
            
        state_data = self._get_state_data(hash_str)
        if state_data:
            state_data["state"] = DownloadState.DELETED.value
            state_data["update_time"] = time.time()
            
            with self._lock:
                # 更新状态
                self._state_cache.set(hash_str, state_data)
                self._persistent_cache.set(f"state_{hash_str}", 
                                          json.dumps(state_data).encode())
                
                # 从活跃下载中移除
                self._active_downloads.discard(hash_str)
                self._save_active_downloads()
            
            # 从待检出缓存中移除
            self._remove_from_pending(state_data)
            
            logger.info(f"标记已删除: {state_data.get('title')} - {hash_str}")
            return state_data
        return None

    def _get_state_data(self, hash_str: str) -> Optional[dict]:
        """获取状态数据，优先从缓存，然后从持久化存储"""
        # 先从短期缓存获取
        state_data = self._state_cache.get(hash_str)
        if state_data:
            return state_data

        # 从持久化存储获取
        try:
            persistent_data = self._persistent_cache.get(f"state_{hash_str}")
            if persistent_data:
                state_data = json.loads(persistent_data.decode())
                # 重新加载到短期缓存
                self._state_cache.set(hash_str, state_data)
                return state_data
        except Exception as e:
            logger.error(f"从持久化存储获取状态失败: {e}")

        return None

    def sync_with_downloader(self, downloader_torrents: List[dict]):
        """与下载器状态同步，解决长时间下载的TTL问题"""
        if not downloader_torrents:
            return

        downloader_hashes = {t.get('hash') for t in downloader_torrents if t.get('hash')}

        # 检查活跃下载状态
        with self._lock:
            active_downloads_copy = self._active_downloads.copy()

        for hash_str in active_downloads_copy:
            state_data = self._get_state_data(hash_str)
            if not state_data:
                continue

            if hash_str in downloader_hashes:
                # 种子仍在下载器中，续期状态
                state_data["last_check"] = time.time()
                state_data["update_time"] = time.time()

                # 检查是否完成下载
                torrent_info = next((t for t in downloader_torrents
                                   if t.get('hash') == hash_str), None)
                if torrent_info and torrent_info.get('progress', 0) >= 100:
                    # 下载完成，更新状态
                    state_data["state"] = DownloadState.COMPLETED.value

                # 更新缓存，相当于续期
                self._state_cache.set(hash_str, state_data)
                self._persistent_cache.set(f"state_{hash_str}",
                                          json.dumps(state_data).encode())

                logger.debug(f"续期下载状态: {state_data.get('title')} - {hash_str}")
            else:
                # 种子不在下载器中，可能被删除
                if state_data.get("state") in [DownloadState.DOWNLOADING.value,
                                              DownloadState.COMPLETED.value]:
                    logger.warn(f"检测到种子可能被删除: {state_data.get('title')} - {hash_str}")
                    # 这里可以选择标记为删除或等待进一步确认

    def _save_active_downloads(self):
        """保存活跃下载列表到持久化存储"""
        try:
            self._persistent_cache.set("active_downloads",
                                      json.dumps(list(self._active_downloads)).encode())
        except Exception as e:
            logger.error(f"保存活跃下载列表失败: {e}")

    def get_pending_episodes(self, tmdbid: int = None, doubanid: str = None,
                           season: int = None) -> List[int]:
        """获取待检出的集数"""
        cache_key = f"{tmdbid or doubanid}_{season or 'movie'}"
        pending_data = self._pending_cache.get(cache_key)
        if pending_data:
            return pending_data.get("episodes", [])
        return []

    def is_media_pending(self, tmdbid: int = None, doubanid: str = None) -> bool:
        """检查媒体是否有待检出内容"""
        cache_key = f"{tmdbid or doubanid}_movie"
        return self._pending_cache.exists(cache_key)

    def is_transfer_completed(self, hash_str: str) -> bool:
        """检查是否已整理完成，解决TTL过期问题"""
        if not hash_str:
            return True

        state_data = self._get_state_data(hash_str)
        if state_data:
            return state_data.get("state") == DownloadState.TRANSFERRED.value

        # 如果缓存中没有记录，检查是否在活跃下载中
        with self._lock:
            if hash_str in self._active_downloads:
                # 在活跃下载中但没有状态数据，可能是异常情况
                logger.warn(f"活跃下载 {hash_str} 缺少状态数据")
                return False

        # 不在活跃下载中，且没有状态记录，认为是老数据，已完成
        return True

    def _update_pending_cache(self, mediainfo: MediaInfo, meta: MetaBase,
                            episodes: List[int], action: str):
        """更新待检出缓存"""
        if mediainfo.type.value == "movie":
            cache_key = f"{mediainfo.tmdb_id or mediainfo.douban_id}_movie"
            if action == "add":
                self._pending_cache.set(cache_key, {"type": "movie"})
        else:
            season = meta.season if meta else 1
            cache_key = f"{mediainfo.tmdb_id or mediainfo.douban_id}_{season}"

            if action == "add":
                existing = self._pending_cache.get(cache_key) or {"episodes": []}
                if episodes:
                    existing["episodes"].extend(episodes)
                    existing["episodes"] = list(set(existing["episodes"]))
                else:
                    # 整季下载
                    existing["full_season"] = True
                self._pending_cache.set(cache_key, existing)

    def _remove_from_pending(self, state_data: dict):
        """从待检出缓存中移除"""
        if state_data.get("type") == "movie":
            cache_key = f"{state_data.get('tmdbid') or state_data.get('doubanid')}_movie"
            self._pending_cache.delete(cache_key)
        else:
            season = state_data.get("season", 1)
            cache_key = f"{state_data.get('tmdbid') or state_data.get('doubanid')}_{season}"

            existing = self._pending_cache.get(cache_key)
            if existing:
                episodes = state_data.get("episodes", [])
                if episodes and "episodes" in existing:
                    for ep in episodes:
                        if ep in existing["episodes"]:
                            existing["episodes"].remove(ep)
                    if not existing["episodes"]:
                        self._pending_cache.delete(cache_key)
                    else:
                        self._pending_cache.set(cache_key, existing)
                else:
                    # 整季完成
                    self._pending_cache.delete(cache_key)

    def cleanup_expired_states(self):
        """清理过期状态"""
        current_time = time.time()
        expired_hashes = []

        with self._lock:
            active_downloads_copy = self._active_downloads.copy()

        for hash_str in active_downloads_copy:
            state_data = self._get_state_data(hash_str)
            if not state_data:
                expired_hashes.append(hash_str)
                continue

            # 超过7天没有更新的状态认为过期
            if current_time - state_data.get("last_check", 0) > 7 * 24 * 3600:
                expired_hashes.append(hash_str)

        # 清理过期状态
        if expired_hashes:
            with self._lock:
                for hash_str in expired_hashes:
                    self._active_downloads.discard(hash_str)
                    self._persistent_cache.delete(f"state_{hash_str}")
                    logger.info(f"清理过期状态: {hash_str}")
                self._save_active_downloads()

    def get_stats(self) -> dict:
        """获取状态统计信息"""
        with self._lock:
            active_count = len(self._active_downloads)

        stats = {
            "active_downloads": active_count,
            "cache_size": len(self._state_cache),
            "pending_cache_size": len(self._pending_cache)
        }

        return stats
