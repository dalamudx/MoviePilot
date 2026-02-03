from enum import Enum
from typing import Dict, List, Optional, Set
from pathlib import Path
import time
import json
import threading

from app.core.cache import Cache, FileCache
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.utils.singleton import SingletonClass
from app.log import logger


class DownloadLifecycle(str, Enum):
    """下载生命周期标记 - 仅用于MoviePilot内部管理"""
    TRANSFERRED = "transferred"    # 已整理完成
    DELETED = "deleted"           # 已删除


class DownloadStateHelper:
    """下载状态辅助工具 - 动态判断下载器状态"""

    @staticmethod
    def is_downloading(torrent_info: dict) -> bool:
        """判断是否正在下载"""
        if not torrent_info:
            return False

        state = (torrent_info.get('state') or '').lower()
        progress = torrent_info.get('progress') or 0

        # 空状态处理
        if not state:
            return False

        # 基于状态关键词判断
        download_keywords = ['download', 'stalled', 'queued', 'paused', 'meta', 'allocat', 'check']
        is_download_state = any(keyword in state for keyword in download_keywords)

        # 特殊处理：stopped状态且进度<100%认为是暂停下载
        if state == 'stopped' and progress < 100:
            return True

        return is_download_state and progress < 100

    @staticmethod
    def is_completed(torrent_info: dict) -> bool:
        """判断是否下载完成（可以开始整理）"""
        if not torrent_info:
            return False

        progress = torrent_info.get('progress') or 0
        state = (torrent_info.get('state') or '').lower()

        # 进度100%或者包含上传/做种关键词
        if progress >= 100:
            return True

        upload_keywords = ['upload', 'seed', 'stalled_up', 'queued_up', 'paused_up']
        return any(keyword in state for keyword in upload_keywords)

    @staticmethod
    def is_error_state(torrent_info: dict) -> bool:
        """判断是否是错误状态"""
        if not torrent_info:
            return True

        state = (torrent_info.get('state') or '').lower()

        # 空状态认为是错误
        if not state:
            return True

        error_keywords = ['error', 'missing', 'unknown']
        return any(keyword in state for keyword in error_keywords)

    @staticmethod
    def get_display_status(torrent_info: dict) -> str:
        """获取用于显示的状态描述"""
        if not torrent_info:
            return "未知"

        state = torrent_info.get('state') or 'unknown'
        progress = torrent_info.get('progress') or 0

        if DownloadStateHelper.is_downloading(torrent_info):
            return f"下载中 ({progress:.1f}%)"
        elif DownloadStateHelper.is_completed(torrent_info):
            return f"已完成 (做种中)"
        elif DownloadStateHelper.is_error_state(torrent_info):
            return f"错误: {state}"
        else:
            return f"{state} ({progress:.1f}%)"


class DownloadStateManager(metaclass=SingletonClass):
    """下载状态管理器"""
    
    def __init__(self):
        # 状态数据缓存
        self._cache = FileCache(base=settings.CACHE_PATH / "download_states")

        # 待处理缓存：改为持久化文件缓存
        self._pending_cache = FileCache(base=settings.CACHE_PATH / "pending_episodes")

        # 活跃下载Hash集合，用于快速判断（内存中维护）
        self._active_downloads: Set[str] = set()

        # 线程锁
        self._lock = threading.RLock()

        # 启动验证标记 - 用于标记是否需要进行启动后的状态验证
        self._startup_verification_needed = True

        # 启动时恢复持久化状态
        self._restore_persistent_states()
    
    def _restore_persistent_states(self):
        """启动时恢复持久化状态"""
        try:
            # 从统一缓存中恢复活跃下载列表
            active_data = self._cache.get("active_downloads")
            if active_data:
                if isinstance(active_data, bytes):
                    restored_hashes = json.loads(active_data.decode())
                else:
                    restored_hashes = active_data

                with self._lock:
                    self._active_downloads.update(restored_hashes)
                logger.info(f"恢复了 {len(restored_hashes)} 个活跃下载状态")
            else:
                logger.info("没有找到持久化的活跃下载状态")
        except Exception as e:
            logger.error(f"恢复持久化状态失败: {e}")
    
    def mark_downloading(self, hash_str: str, mediainfo: MediaInfo,
                        meta: MetaBase, episodes: List[int] = None):
        """标记为下载中，支持长期存储"""
        if not hash_str or not mediainfo:
            return

        state_data = {
            "hash": hash_str,
            "state": "downloading",  # 初始状态，后续会从下载器动态更新
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
            # 统一存储到缓存
            self._cache.set(f"state_{hash_str}", json.dumps(state_data).encode())

            # 添加到活跃下载集合
            self._active_downloads.add(hash_str)
            self._save_active_downloads()
        
        # 更新待处理缓存
        self._update_pending_cache(mediainfo, meta, episodes, "add")
        
        logger.info(f"标记下载中: {mediainfo.title_year} - {hash_str}")
    
    def is_torrent_fully_downloaded(self, hash_str: str, torrent_info: dict = None) -> bool:
        """检查种子是否完整下载完成"""
        if not hash_str:
            return False

        # 如果没有提供种子信息，返回False（保守策略）
        if not torrent_info:
            return False

        # 检查下载进度
        progress = torrent_info.get('progress') or 0
        if progress < 100:
            return False

        # 检查种子状态
        state = (torrent_info.get('state') or '').lower()

        # 只有在完成状态或做种状态才认为下载完成
        # 包含所有Qbittorrent已完成下载的状态（所有*UP状态 + 通用状态）
        completed_states = [
            'completed',    # 通用完成状态
            'seeding',      # 通用做种状态
            'uploading',    # 正在做种并传输数据
            'stalledup',    # 正在做种但无连接
            'queuedup',     # 队列中等待上传
            'pausedup',     # 已暂停且已完成下载
            'forcedup',     # 强制上传忽略队列
            'stoppedup',    # 已停止上传（达到分享率等原因）
        ]
        if state not in completed_states:
            return False

        return True

    def mark_transferred(self, hash_str: str):
        """标记为已整理完成"""
        if not hash_str:
            return

        state_data = self._get_state_data(hash_str)
        if state_data:
            state_data["state"] = DownloadLifecycle.TRANSFERRED.value
            state_data["update_time"] = time.time()
            state_data["transfer_time"] = time.time()

            with self._lock:
                # 更新缓存
                self._cache.set(f"state_{hash_str}", json.dumps(state_data).encode())

                # 从活跃下载中移除
                self._active_downloads.discard(hash_str)
                self._save_active_downloads()

            # 从待处理缓存中移除
            self._remove_from_pending(state_data)

            logger.info(f"标记已整理: {state_data.get('title')} - {hash_str}")

    def should_start_transfer(self, hash_str: str, torrent_info: dict = None) -> bool:
        """判断是否应该开始整理（必须完整下载完成）"""
        if not hash_str:
            return False

        # 检查种子是否完整下载完成
        if not self.is_torrent_fully_downloaded(hash_str, torrent_info):
            logger.debug(f"种子 {hash_str} 尚未完整下载完成，跳过整理")
            return False

        # 检查是否已经整理过
        if self.is_transfer_completed(hash_str):
            logger.debug(f"种子 {hash_str} 已经整理完成，跳过")
            return False

        return True

    def get_transfer_block_reason(self, hash_str: str, torrent_info: dict = None) -> str:
        """获取种子无法整理的具体原因"""
        if not hash_str:
            return "种子Hash为空"

        if not torrent_info:
            return "缺少种子信息"

        # 检查下载进度
        progress = torrent_info.get('progress') or 0
        if progress < 100:
            return f"下载未完成 ({progress:.1f}%)"

        # 检查种子状态
        state = (torrent_info.get('state') or '').lower()
        completed_states = ['completed', 'seeding', 'uploading', 'stalledup', 'queuedup', 'pausedup', 'forcedup']
        if state not in completed_states:
            return f"种子状态不正确 ({state})"

        # 检查是否已经整理过
        if self.is_transfer_completed(hash_str):
            return "已经整理完成"

        return "未知原因"

    def batch_check_transferable(self, torrent_infos: dict) -> dict:
        """批量检查种子是否可以整理

        Args:
            torrent_infos: {hash: torrent_info} 格式的字典

        Returns:
            {hash: (can_transfer, reason)} 格式的字典
        """
        results = {}

        # 批量获取已整理状态
        transferred_hashes = set()
        for hash_str in torrent_infos.keys():
            if self.is_transfer_completed(hash_str):
                transferred_hashes.add(hash_str)

        # 逐个检查
        for hash_str, torrent_info in torrent_infos.items():
            if not hash_str:
                results[hash_str] = (False, "种子Hash为空")
                continue

            if not torrent_info:
                results[hash_str] = (False, "缺少种子信息")
                continue

            # 检查下载完成状态
            if not self.is_torrent_fully_downloaded(hash_str, torrent_info):
                progress = torrent_info.get('progress') or 0
                state = torrent_info.get('state') or 'unknown'
                if progress < 100:
                    results[hash_str] = (False, f"下载未完成 ({progress:.1f}%)")
                else:
                    results[hash_str] = (False, f"种子状态不正确 ({state})")
                continue

            # 检查是否已整理
            if hash_str in transferred_hashes:
                results[hash_str] = (False, "已经整理完成")
                continue

            # 可以整理
            results[hash_str] = (True, "可以整理")

        return results

    def get_error_recovery_suggestions(self, hash_str: str, torrent_info: dict = None) -> List[str]:
        """获取错误恢复建议"""
        suggestions = []

        if not hash_str:
            suggestions.append("提供有效的种子Hash")
            return suggestions

        if not torrent_info:
            suggestions.append("检查下载器连接，获取种子信息")
            return suggestions

        progress = torrent_info.get('progress') or 0
        state = (torrent_info.get('state') or '').lower()

        if progress < 100:
            suggestions.append(f"等待下载完成 (当前进度: {progress:.1f}%)")
            if state in ['paused', 'pauseddl']:
                suggestions.append("恢复下载 (当前已暂停)")
            elif state in ['error']:
                suggestions.append("检查下载错误，重新开始下载")
            elif state in ['stalledDL']:
                suggestions.append("检查网络连接和种子健康度")

        elif state not in ['completed', 'seeding', 'uploading', 'stalledup', 'queuedup', 'pausedup', 'forcedup']:
            suggestions.append(f"检查种子状态异常: {state}")
            suggestions.append("尝试重新启动下载器")

        # 检查Redis状态
        state_data = self._get_state_data(hash_str)
        if state_data and state_data.get('state') == 'transferred':
            suggestions.append("种子已标记为已整理，如需重新整理请重置状态")

        if not suggestions:
            suggestions.append("种子状态正常，可以开始整理")

        return suggestions

    def mark_deleted(self, hash_str: str):
        """标记为已删除（直接清理数据）"""
        if not hash_str:
            return None

        state_data = self._get_state_data(hash_str)
        if state_data:
            with self._lock:
                # 直接删除缓存（不再保留 deleted 状态历史）
                self._cache.delete(f"state_{hash_str}")
                
                # 从活跃下载中移除
                self._active_downloads.discard(hash_str)
                self._save_active_downloads()

            # 从待处理缓存中移除
            self._remove_from_pending(state_data)

            logger.info(f"删除状态: {state_data.get('title')} - {hash_str}")
            return state_data
        return None

    def _get_state_data(self, hash_str: str) -> Optional[dict]:
        """从统一缓存获取状态数据"""
        try:
            cache_data = self._cache.get(f"state_{hash_str}")
            if cache_data:
                return json.loads(cache_data.decode())
        except Exception as e:
            logger.error(f"从缓存获取状态失败: {e}")

        return None

    def sync_with_downloader(self, downloader_torrents: List[dict]):
        """与下载器状态同步，解决长时间下载的TTL问题，并进行状态验证和清理"""
        downloader_hashes = {t.get('hash') for t in downloader_torrents if t.get('hash')} if downloader_torrents else set()

        # 如果是启动后首次同步，进行完整的状态验证
        if self._startup_verification_needed:
            logger.info("执行启动后的下载状态验证和清理...")
            self._perform_startup_verification(downloader_hashes, downloader_torrents or [])
            self._startup_verification_needed = False
            return

        # 检查活跃下载状态
        with self._lock:
            active_downloads_copy = self._active_downloads.copy()

        updated_count = 0
        cleaned_count = 0

        for hash_str in active_downloads_copy:
            state_data = self._get_state_data(hash_str)
            if not state_data:
                # 没有状态数据的活跃下载，直接清理
                logger.warn(f"活跃下载 {hash_str} 缺少状态数据，自动清理")
                self._cleanup_single_state(hash_str)
                cleaned_count += 1
                continue

            if hash_str in downloader_hashes:
                # 种子仍在下载器中，续期状态
                state_data["last_check"] = time.time()
                state_data["update_time"] = time.time()

                # 检查并更新下载器状态
                torrent_info = next((t for t in downloader_torrents
                                   if t.get('hash') == hash_str), None)
                if torrent_info:
                    # 获取下载器原生状态
                    raw_state = torrent_info.get('state', 'unknown')
                    raw_progress = torrent_info.get('progress', 0)

                    # 总是以下载器数据为准，更新状态和进度
                    old_state = state_data.get("state", "unknown")
                    if old_state != raw_state:
                        state_data["state"] = raw_state
                        logger.info(f"同步下载器状态: {state_data.get('title')} - {hash_str}: {old_state} -> {raw_state}")

                    # 更新进度信息
                    state_data["progress"] = raw_progress

                # 更新缓存，相当于续期
                self._cache.set(f"state_{hash_str}", json.dumps(state_data).encode())

                logger.debug(f"续期下载状态: {state_data.get('title')} - {hash_str}")
                updated_count += 1
            else:
                # 种子不在下载器中，自动清理状态
                current_state = state_data.get("state", "")
                title = state_data.get('title', '未知')

                # 以下载器为准：种子不在下载器中就清理状态（不保护任何状态）
                logger.info(f"种子已从下载器中移除，自动清理状态: {title} - {hash_str} (原状态: {current_state})")
                self.mark_deleted(hash_str)
                cleaned_count += 1

        if updated_count > 0 or cleaned_count > 0:
            logger.info(f"下载状态同步完成: 更新 {updated_count} 个，清理 {cleaned_count} 个")

    def _perform_startup_verification(self, downloader_hashes: set, downloader_torrents: List[dict]):
        """执行启动后的完整状态验证"""
        with self._lock:
            active_downloads_copy = self._active_downloads.copy()

        if not active_downloads_copy:
            logger.info("没有需要验证的活跃下载状态")
            return

        logger.info(f"开始验证 {len(active_downloads_copy)} 个活跃下载状态...")

        valid_count = 0
        cleaned_count = 0
        updated_count = 0

        for hash_str in active_downloads_copy:
            state_data = self._get_state_data(hash_str)

            if not state_data:
                # 缺少状态数据，清理
                logger.warn(f"活跃下载 {hash_str} 缺少状态数据，清理")
                self._cleanup_single_state(hash_str)
                cleaned_count += 1
                continue

            title = state_data.get('title', '未知')
            current_state = state_data.get("state", "")

            # 检查种子是否还在下载器中（不再预先清理已完成状态）

            if hash_str in downloader_hashes:
                # 种子存在，以下载器数据为准更新状态
                state_data["last_check"] = time.time()
                state_data["update_time"] = time.time()

                # 检查并更新下载器状态
                torrent_info = next((t for t in downloader_torrents if t.get('hash') == hash_str), None)
                if torrent_info:
                    raw_state = torrent_info.get('state', 'unknown')
                    raw_progress = torrent_info.get('progress', 0)

                    # 总是以下载器数据为准更新状态
                    old_state = state_data.get("state", "unknown")
                    if old_state != raw_state:
                        state_data["state"] = raw_state
                        logger.info(f"启动验证同步状态: {title} - {hash_str}: {old_state} -> {raw_state}")
                        updated_count += 1

                    # 更新进度信息
                    state_data["progress"] = raw_progress

                # 更新缓存
                self._cache.set(f"state_{hash_str}", json.dumps(state_data).encode())

                valid_count += 1
                logger.debug(f"验证通过: {title} - {hash_str}")
            else:
                # 种子不在下载器中，以下载器为准清理状态（不保护任何状态）
                logger.info(f"启动验证发现种子已被移除，清理状态: {title} - {hash_str} (原状态: {current_state})")
                self.mark_deleted(hash_str)
                cleaned_count += 1

        logger.info(f"启动状态验证完成: 有效 {valid_count} 个，更新 {updated_count} 个，清理 {cleaned_count} 个")

    def _cleanup_single_state(self, hash_str: str):
        """清理单个状态"""
        try:
            with self._lock:
                self._active_downloads.discard(hash_str)
                self._cache.delete(f"state_{hash_str}")
                self._save_active_downloads()
        except Exception as e:
            logger.error(f"清理状态 {hash_str} 失败: {e}")

    def _save_active_downloads(self):
        """保存活跃下载列表到统一缓存"""
        try:
            self._cache.set("active_downloads",
                           json.dumps(list(self._active_downloads)).encode())
        except Exception as e:
            logger.error(f"保存活跃下载列表失败: {e}")

    def get_pending_episodes(self, tmdbid: int = None, doubanid: str = None,
                           season: int = None, totals: int = None) -> List[int]:
        """获取待处理的集数"""
        if season and season != "movie":
            try:
                season = int(season)
            except (ValueError, TypeError):
                 import re
                 match = re.search(r'\d+', str(season))
                 season = int(match.group()) if match else 1

        cache_key = f"{tmdbid or doubanid}_{season or 'movie'}"
        # 从 FileCache 读取
        cached_data = self._pending_cache.get(cache_key)
        
        # DEBUG LOG
        logger.debug(f"查询待处理集数: Key={cache_key}, RawData={cached_data}")

        if not cached_data:
            return []

        try:
            pending_info = json.loads(cached_data.decode())
            # 检查有效期
            if time.time() - pending_info.get("timestamp", 0) > 7 * 24 * 3600:
                logger.debug(f"待处理缓存过期: Key={cache_key}")
                self._pending_cache.delete(cache_key)
                return []
                
            episodes = pending_info.get("episodes", [])
            
            # 如果是整季下载，返回所有集数
            if pending_info.get("full_season"):
                if totals:
                    episodes = list(range(1, totals + 1))
                else:
                    # 整季下载但未知总集数，返回None表示全部
                    return None
            logger.debug(f"待处理集数: Key={cache_key}, Episodes={episodes}")
            return episodes
        except Exception as e:
            logger.error(f"解析待处理缓存失败: {e}")
            return []

    def is_media_pending(self, tmdbid: int = None, doubanid: str = None,
                        media_type: str = "movie", season: int = None) -> bool:
        """检查媒体是否有待处理内容"""
        if media_type == "movie":
            cache_key = f"{tmdbid or doubanid}_movie"
        else:
            cache_key = f"{tmdbid or doubanid}_{season or 1}"
        return self._pending_cache.exists(cache_key)



    def is_transfer_completed(self, hash_str: str) -> bool:
        """检查是否已整理完成，解决TTL过期问题"""
        if not hash_str:
            return True

        state_data = self._get_state_data(hash_str)
        if state_data:
            return state_data.get("state") == DownloadLifecycle.TRANSFERRED.value

        # 如果缓存中没有记录，检查是否在活跃下载中
        with self._lock:
            if hash_str in self._active_downloads:
                # 在活跃下载中但没有状态数据，可能是异常情况
                logger.warn(f"活跃下载 {hash_str} 缺少状态数据")
                return False

        # 不在活跃下载中，且没有状态记录，认为未被跟踪，允许整理
        return False

    def _update_pending_cache(self, mediainfo: MediaInfo, meta: MetaBase,
                            episodes: List[int], action: str):
        """更新待处理缓存"""
        if mediainfo.type.value == "movie":
            cache_key = f"{mediainfo.tmdb_id or mediainfo.douban_id}_movie"
            if action == "add":
                data = {
                    "type": "movie",
                    "timestamp": time.time()
                }
                self._pending_cache.set(cache_key, json.dumps(data).encode())
                logger.debug(f"添加待处理缓存(Movie): Key={cache_key}")
        else:
            # ⭐ 强制转换为 int 去除 "S01" 这种格式差异
            try:
                season = int(meta.season) if meta and meta.season else 1
            except (ValueError, TypeError):
                # 如果包含非数字字符（如 "S01"），尝试提取数字
                import re
                match = re.search(r'\d+', str(meta.season)) if meta and meta.season else None
                season = int(match.group()) if match else 1
            
            cache_key = f"{mediainfo.tmdb_id or mediainfo.douban_id}_{season}"

            if action == "add":
                # 从 FileCache 读取
                cached_data = self._pending_cache.get(cache_key)
                if cached_data:
                    try:
                        existing = json.loads(cached_data.decode())
                    except Exception:
                        existing = {"episodes": []}
                else:
                    existing = {"episodes": []}
                
                if episodes:
                    if "episodes" not in existing:
                        existing["episodes"] = []
                    existing["episodes"].extend(episodes)
                    existing["episodes"] = list(set(existing["episodes"]))
                else:
                    # 整季下载
                    existing["full_season"] = True
                
                existing["timestamp"] = time.time()
                self._pending_cache.set(cache_key, json.dumps(existing).encode())
                logger.debug(f"更新待处理缓存(TV): Action=Add, Key={cache_key}, Episodes={episodes}, NewData={existing}")

    def _remove_from_pending(self, state_data: dict):
        """从待处理缓存中移除"""
        if state_data.get("type") == "movie":
            cache_key = f"{state_data.get('tmdbid') or state_data.get('doubanid')}_movie"
            self._pending_cache.delete(cache_key)
            logger.debug(f"移除待处理缓存(Movie): Key={cache_key}")
        else:
            try:
                season = int(state_data.get("season", 1))
            except (ValueError, TypeError):
                import re
                match = re.search(r'\d+', str(state_data.get("season", 1)))
                season = int(match.group()) if match else 1
                
            cache_key = f"{state_data.get('tmdbid') or state_data.get('doubanid')}_{season}"

            cached_data = self._pending_cache.get(cache_key)
            if cached_data:
                try:
                    existing = json.loads(cached_data.decode())
                except Exception:
                    self._pending_cache.delete(cache_key)
                    return

                episodes = state_data.get("episodes", [])
                logger.debug(f"移除待处理缓存(TV): Key={cache_key}, ToRemove={episodes}, Current={existing}")
                
                if episodes and "episodes" in existing:
                    for ep in episodes:
                        if ep in existing["episodes"]:
                            existing["episodes"].remove(ep)
                    
                    if not existing["episodes"]:
                        self._pending_cache.delete(cache_key)
                        logger.debug(f"移除待处理缓存(TV): Key={cache_key} Removed (Empty)")
                    else:
                        existing["timestamp"] = time.time()
                        self._pending_cache.set(cache_key, json.dumps(existing).encode())
                        logger.debug(f"移除待处理缓存(TV): Key={cache_key} Updated: {existing}")
                else:
                    # 整季完成
                    self._pending_cache.delete(cache_key)
                    logger.debug(f"移除待处理缓存(TV): Key={cache_key} Removed (Full Season)")

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
                    self._cache.delete(f"state_{hash_str}")
                    logger.info(f"清理过期状态: {hash_str}")
                self._save_active_downloads()

    def get_stats(self) -> dict:
        """获取状态统计信息"""
        with self._lock:
            active_count = len(self._active_downloads)

        stats = {
            "active_downloads": active_count,
            "cache_size": len(self._cache),
            "pending_cache_size": len(self._pending_cache)
        }

        return stats

    def get_active_downloads(self) -> List[dict]:
        """获取活跃下载列表的详细信息"""
        active_downloads = []

        with self._lock:
            active_hashes = self._active_downloads.copy()

        for hash_str in active_hashes:
            state_data = self._get_state_data(hash_str)
            if state_data:
                active_downloads.append({
                    "hash": hash_str,
                    "title": state_data.get("title", "未知"),
                    "year": state_data.get("year"),
                    "type": state_data.get("type", "unknown"),
                    "season": state_data.get("season"),
                    "episodes": state_data.get("episodes", []),
                    "state": state_data.get("state", "unknown"),
                    "tmdbid": state_data.get("tmdbid"),
                    "doubanid": state_data.get("doubanid"),
                    "start_time": state_data.get("start_time"),
                    "update_time": state_data.get("update_time"),
                    "last_check": state_data.get("last_check")
                })

        return active_downloads

    def get_active_download_hashes(self) -> List[str]:
        """获取活跃下载的Hash列表"""
        with self._lock:
            return list(self._active_downloads)

    def _get_all_transferred_hashes(self) -> List[str]:
        """获取所有已标记为已整理的种子Hash列表"""
        transferred_hashes = []
        
        # 遍历所有可能的状态缓存键
        try:
            # 获取所有缓存键
            all_keys = list(self._cache.keys())
            for key in all_keys:
                if not key.startswith("state_"):
                    continue
                
                state_data = self._cache.get(key)
                if state_data:
                    try:
                        data = json.loads(state_data.decode())
                        if data.get("state") == DownloadLifecycle.TRANSFERRED.value:
                            transferred_hashes.append(data.get("hash"))
                    except:
                        continue
        except Exception as e:
            logger.error(f"获取已整理Hash列表失败: {e}")
        
        return transferred_hashes

    def get_transferred_episodes(self, tmdbid: int = None, doubanid: str = None, 
                                season: int = None, media_type: str = "tv") -> List[int]:
        """
        获取已整理完成的集数
        
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣 ID
        :param season: 季数（电视剧）
        :param media_type: 媒体类型 "tv" 或 "movie"
        :return: 已整理完成的集数列表
        """
        if not tmdbid and not doubanid:
            return []
        
        transferred_episodes = []
        
        # 遍历所有已整理的下载
        for hash_str in self._get_all_transferred_hashes():
            state_data = self._get_state_data(hash_str)
            if not state_data:
                continue
            
            # 匹配媒体信息
            match_tmdb = tmdbid and state_data.get("tmdbid") == tmdbid
            match_douban = doubanid and state_data.get("doubanid") == doubanid
            
            if not (match_tmdb or match_douban):
                continue
            
            # 电视剧：匹配季数
            if media_type == "tv":
                if season and state_data.get("season") != season:
                    continue
                episodes = state_data.get("episodes", [])
                transferred_episodes.extend(episodes)
            
            # 电影：添加 [1]
            elif media_type == "movie":
                transferred_episodes.append(1)
        
        # 去重并排序
        return sorted(list(set(transferred_episodes)))

    def cleanup_media_states(self, tmdbid: int = None, doubanid: str = None, 
                            season: int = None, media_type: str = "tv") -> int:
        """
        清理指定媒体的所有状态数据（订阅完成时调用）
        
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣 ID
        :param season: 季数（电视剧）
        :param media_type: 媒体类型 "tv" 或 "movie"
        :return: 清理的记录数
        """
        if not tmdbid and not doubanid:
            return 0
        
        cleaned_count = 0
        hashes_to_delete = []
        
        # 获取所有状态键
        try:
            all_keys = list(self._cache.keys())
            for key in all_keys:
                if not key.startswith("state_"):
                    continue
                
                hash_str = key.replace("state_", "")
                state_data = self._get_state_data(hash_str)
                if not state_data:
                    continue
                
                # 匹配媒体信息
                match_tmdb = tmdbid and state_data.get("tmdbid") == tmdbid
                match_douban = doubanid and state_data.get("doubanid") == doubanid
                
                if not (match_tmdb or match_douban):
                    continue
                
                # 电视剧：匹配季数
                if media_type == "tv":
                    if season and state_data.get("season") != season:
                        continue
                
                # 记录需要删除的Hash
                hashes_to_delete.append(hash_str)
            
            # 执行批量删除
            with self._lock:
                for hash_str in hashes_to_delete:
                    self._cache.delete(f"state_{hash_str}")
                    self._active_downloads.discard(hash_str)
                    cleaned_count += 1
                
                if hashes_to_delete:
                    self._save_active_downloads()
            
            if cleaned_count > 0:
                logger.info(f"清理媒体状态数据: TMDB={tmdbid}, 豆瓣={doubanid}, 季={season}, 共 {cleaned_count} 条")
        
        except Exception as e:
            logger.error(f"清理媒体状态数据失败: {e}")
        
        return cleaned_count
