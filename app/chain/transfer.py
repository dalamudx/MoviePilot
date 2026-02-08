import queue
import re
import threading
import traceback
from copy import deepcopy
from pathlib import Path
from typing import List, Optional, Tuple, Union, Dict, Callable

from app import schemas
from app.chain import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings, global_vars
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.format import FormatParser
from app.helper.progress import ProgressHelper
from app.log import logger
from app.schemas import StorageOperSelectionEventData
from app.schemas import TransferInfo, Notification, EpisodeFormat, FileItem, TransferDirectoryConf, \
    TransferTask, TransferQueue, TransferJob, TransferJobTask
from app.schemas.exception import OperationInterrupted
from app.schemas.types import TorrentStatus, EventType, MediaType, ProgressKey, NotificationType, MessageChannel, \
    SystemConfigKey, ChainEventType, ContentType
from app.utils.mixins import ConfigReloadMixin
from app.utils.singleton import Singleton
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

# 下载器锁
downloader_lock = threading.Lock()
# 作业锁
job_lock = threading.Lock()
# 任务锁
task_lock = threading.Lock()


class TransferMetrics:
    """整理指标收集器"""
    
    def __init__(self):
        import time
        self.torrents_processed = 0
        self.files_transferred = 0
        self.files_skipped = 0
        self.files_failed = 0
        self.total_size_bytes = 0
        self.lock_contentions = 0  # 锁竞争次数
        self.subscribe_updates = 0  # 订阅更新次数
        self.start_time = time.time()
    
    def to_dict(self):
        """转换为字典格式"""
        import time
        duration = time.time() - self.start_time
        return {
            "torrents_processed": self.torrents_processed,
            "files_transferred": self.files_transferred,
            "files_skipped": self.files_skipped,
            "files_failed": self.files_failed,
            "total_size_mb": round(self.total_size_bytes / 1024 / 1024, 2),
            "duration_seconds": round(duration, 2),
            "lock_contentions": self.lock_contentions,
            "subscribe_updates": self.subscribe_updates,
        }


class JobManager:
    """
    作业管理器
    task任务负责一个文件的整理，job作业负责一个媒体的整理
    """

    # 整理中的作业
    _job_view: Dict[Tuple, TransferJob] = {}
    # 汇总季集清单
    _season_episodes: Dict[Tuple, List[int]] = {}

    def __init__(self):
        self._job_view = {}
        self._season_episodes = {}

    @staticmethod
    def __get_meta_id(meta: MetaBase = None, season: Optional[int] = None) -> Tuple:
        """
        获取元数据ID
        """
        return meta.name, season

    @staticmethod
    def __get_media_id(media: MediaInfo = None, season: Optional[int] = None) -> Tuple:
        """
        获取媒体ID
        """
        if not media:
            return None, season
        return media.tmdb_id or media.douban_id, season

    def __get_id(self, task: TransferTask = None) -> Tuple:
        """
        获取作业ID
        """
        if task.mediainfo:
            return self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)
        else:
            return self.__get_meta_id(meta=task.meta, season=task.meta.begin_season)

    @staticmethod
    def __get_media(task: TransferTask) -> schemas.MediaInfo:
        """
        获取媒体信息
        """
        if task.mediainfo:
            # 有媒体信息
            mediainfo = deepcopy(task.mediainfo)
            mediainfo.clear()
            return schemas.MediaInfo(**mediainfo.to_dict())
        else:
            # 没有媒体信息
            meta: MetaBase = task.meta
            return schemas.MediaInfo(
                title=meta.name,
                year=meta.year,
                title_year=f"{meta.name} ({meta.year})",
                type=meta.type.value if meta.type else None
            )

    @staticmethod
    def __get_meta(task: TransferTask) -> schemas.MetaInfo:
        """
        获取元数据
        """
        return schemas.MetaInfo(**task.meta.to_dict())

    def add_task(self, task: TransferTask, state: Optional[str] = "waiting") -> bool:
        """
        添加整理任务，自动分组到对应的作业中
        :return: True表示任务已添加，False表示任务无效或已存在（重复）
        """
        if not all([task, task.meta, task.fileitem]):
            return False
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                self._job_view[__mediaid__] = TransferJob(
                    media=self.__get_media(task),
                    season=task.meta.begin_season,
                    tasks=[TransferJobTask(
                        fileitem=task.fileitem,
                        meta=self.__get_meta(task),
                        downloader=task.downloader,
                        download_hash=task.download_hash,
                        state=state
                    )]
                )
            else:
                # 不重复添加任务
                if any([t.fileitem == task.fileitem for t in self._job_view[__mediaid__].tasks]):
                    logger.debug(f"任务 {task.fileitem.name} 已存在，跳过重复添加")
                    return False
                self._job_view[__mediaid__].tasks.append(
                    TransferJobTask(
                        fileitem=task.fileitem,
                        meta=self.__get_meta(task),
                        downloader=task.downloader,
                        download_hash=task.download_hash,
                        state=state
                    )
                )
            # 添加季集信息
            if self._season_episodes.get(__mediaid__):
                self._season_episodes[__mediaid__].extend(task.meta.episode_list)
                self._season_episodes[__mediaid__] = list(set(self._season_episodes[__mediaid__]))
            else:
                self._season_episodes[__mediaid__] = task.meta.episode_list
            return True

    def running_task(self, task: TransferTask):
        """
        设置任务为运行中
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in self._job_view[__mediaid__].tasks:
                if t.fileitem == task.fileitem:
                    t.state = "running"
                    break

    def finish_task(self, task: TransferTask):
        """
        设置任务为完成/成功
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in self._job_view[__mediaid__].tasks:
                if t.fileitem == task.fileitem:
                    t.state = "completed"
                    break

    def fail_task(self, task: TransferTask):
        """
        设置任务为失败
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in self._job_view[__mediaid__].tasks:
                if t.fileitem == task.fileitem:
                    t.state = "failed"
                    break
            # 移除剧集信息
            if __mediaid__ in self._season_episodes:
                self._season_episodes[__mediaid__] = list(
                    set(self._season_episodes[__mediaid__]) - set(task.meta.episode_list)
                )

    def remove_task(self, fileitem: FileItem) -> Optional[TransferJobTask]:
        """
        根据文件项移除任务
        """
        with job_lock:
            for mediaid in list(self._job_view):
                job = self._job_view[mediaid]
                for task in job.tasks:
                    if task.fileitem == fileitem:
                        job.tasks.remove(task)
                        # 如果没有作业了，则移除作业
                        if not job.tasks:
                            self._job_view.pop(mediaid)
                        # 移除季集信息
                        if mediaid in self._season_episodes:
                            self._season_episodes[mediaid] = list(
                                set(self._season_episodes[mediaid]) - set(task.meta.episode_list)
                            )
                        return task
            return None

    def remove_job(self, task: TransferTask) -> Optional[TransferJob]:
        """
        移除任务对应的作业（强制，线程不安全）
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ in self._job_view:
                # 移除季集信息
                if __mediaid__ in self._season_episodes:
                    self._season_episodes.pop(__mediaid__)
                return self._job_view.pop(__mediaid__)
            return None

    def try_remove_job(self, task: TransferTask):
        """
        尝试移除任务对应的作业（严格检查未完成作业，线程安全）
        """
        with job_lock:
            __metaid__ = self.__get_meta_id(meta=task.meta, season=task.meta.begin_season)
            __mediaid__ = self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)

            meta_done = True
            if __metaid__ in self._job_view:
                meta_done = all(
                    t.state in ["completed", "failed"] for t in self._job_view[__metaid__].tasks
                )

            media_done = True
            if __mediaid__ in self._job_view:
                media_done = all(
                    t.state in ["completed", "failed"] for t in self._job_view[__mediaid__].tasks
                )

            if meta_done and media_done:
                __id__ = self.__get_id(task)
                if __id__ in self._job_view:
                    # 移除季集信息
                    if __id__ in self._season_episodes:
                        self._season_episodes.pop(__id__)
                    self._job_view.pop(__id__)

    def is_done(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否整理完成（不管成功还是失败）
        """
        with job_lock:
            __metaid__ = self.__get_meta_id(meta=task.meta, season=task.meta.begin_season)
            __mediaid__ = self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)
            if __metaid__ in self._job_view:
                meta_done = all(
                    task.state in ["completed", "failed"] for task in self._job_view[__metaid__].tasks
                )
            else:
                meta_done = True
            if __mediaid__ in self._job_view:
                media_done = all(
                    task.state in ["completed", "failed"] for task in self._job_view[__mediaid__].tasks
                )
            else:
                media_done = True
            return meta_done and media_done

    def is_finished(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否已完成且有成功的记录
        """
        with job_lock:
            __metaid__ = self.__get_meta_id(meta=task.meta, season=task.meta.begin_season)
            __mediaid__ = self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)
            if __metaid__ in self._job_view:
                meta_finished = all(
                    task.state in ["completed", "failed"] for task in self._job_view[__metaid__].tasks
                )
            else:
                meta_finished = True
            if __mediaid__ in self._job_view:
                tasks = self._job_view[__mediaid__].tasks
                media_finished = all(
                    task.state in ["completed", "failed"] for task in tasks
                ) and any(
                    task.state == "completed" for task in tasks
                )
            else:
                media_finished = True
            return meta_finished and media_finished

    def is_success(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否全部成功
        """
        with job_lock:
            __metaid__ = self.__get_meta_id(meta=task.meta, season=task.meta.begin_season)
            __mediaid__ = self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)
            if __metaid__ in self._job_view:
                meta_success = all(
                    task.state in ["completed"] for task in self._job_view[__metaid__].tasks
                )
            else:
                meta_success = True
            if __mediaid__ in self._job_view:
                media_success = all(
                    task.state in ["completed"] for task in self._job_view[__mediaid__].tasks
                )
            else:
                media_success = True
            return meta_success and media_success

    def get_all_torrent_hashes(self) -> set[str]:
        """
        获取所有种子的哈希值集合
        """
        with job_lock:
            return {
                task.download_hash
                for job in self._job_view.values()
                for task in job.tasks
            }

    def is_torrent_done(self, download_hash: str) -> bool:
        """
        检查指定种子的所有任务是否都已完成
        """
        with job_lock:
            if any(
                task.state not in {"completed", "failed"}
                for job in self._job_view.values()
                for task in job.tasks
                if task.download_hash == download_hash
            ):
                return False
            return True

    def is_torrent_success(self, download_hash: str) -> bool:
        """
        检查指定种子的所有任务是否都已成功
        """
        with job_lock:
            if any(
                task.state != "completed"
                for job in self._job_view.values()
                for task in job.tasks
                if task.download_hash == download_hash
            ):
                return False
            return True

    def has_tasks(self, meta: MetaBase, mediainfo: Optional[MediaInfo] = None, season: Optional[int] = None) -> bool:
        """
        判断作业是否还有任务正在处理
        """
        with job_lock:
            if mediainfo:
                __mediaid__ = self.__get_media_id(media=mediainfo, season=season)
                if __mediaid__ in self._job_view:
                    return True

            __metaid__ = self.__get_meta_id(meta=meta, season=season)
            return __metaid__ in self._job_view and len(self._job_view[__metaid__].tasks) > 0

    def success_tasks(self, media: MediaInfo, season: Optional[int] = None) -> List[TransferJobTask]:
        """
        获取作业中所有成功的任务
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return []
            return [task for task in self._job_view[__mediaid__].tasks if task.state == "completed"]

    def all_tasks(self, media: MediaInfo, season: Optional[int] = None) -> List[TransferJobTask]:
        """
        获取作业中全部任务
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return []
            return self._job_view[__mediaid__].tasks

    def count(self, media: MediaInfo, season: Optional[int] = None) -> int:
        """
        获取作业中成功总数
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return 0
            return len([task for task in self._job_view[__mediaid__].tasks if task.state == "completed"])

    def size(self, media: MediaInfo, season: Optional[int] = None) -> int:
        """
        获取作业中所有成功文件总大小
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return 0
            return sum([
                task.fileitem.size if task.fileitem.size is not None
                else (
                    SystemUtils.get_directory_size(Path(task.fileitem.path)) if task.fileitem.storage == "local" else 0)
                for task in self._job_view[__mediaid__].tasks
                if task.state == "completed"
            ])

    def total(self) -> int:
        """
        获取所有任务总数
        """
        with job_lock:
            return sum([len(job.tasks) for job in self._job_view.values()])

    def list_jobs(self) -> List[TransferJob]:
        """
        获取所有作业的任务列表
        """
        with job_lock:
            return list(self._job_view.values())

    def season_episodes(self, media: MediaInfo, season: Optional[int] = None) -> List[int]:
        """
        获取作业的季集清单
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            return self._season_episodes.get(__mediaid__) or []


class TransferChain(ChainBase, ConfigReloadMixin, metaclass=Singleton):
    """
    文件整理处理链
    """

    CONFIG_WATCH = {
        "TRANSFER_THREADS",
    }

    def __init__(self):
        super().__init__()
        # 主要媒体文件后缀
        self._media_exts = settings.RMT_MEDIAEXT
        # 字幕文件后缀
        self._subtitle_exts = settings.RMT_SUBEXT
        # 音频文件后缀
        self._audio_exts = settings.RMT_AUDIOEXT
        # 可处理的文件后缀（视频文件、字幕、音频文件）
        self._allowed_exts = self._media_exts + self._audio_exts + self._subtitle_exts
        # 待整理任务队列
        self._queue = queue.Queue()
        # 文件整理线程
        self._transfer_threads = []
        # 队列间隔时间（秒）
        self._transfer_interval = 15
        # 事件管理器
        self.jobview = JobManager()
        # 转移成功的文件清单
        self._success_target_files: Dict[str, List[str]] = {}
        # 导入状态管理器
        from app.core.download_state import DownloadStateManager
        self._state_manager = DownloadStateManager()
        # 整理进度
        self._progress = ProgressHelper(ProgressKey.FileTransfer)
        # 队列相关状态
        self._threads = []
        self._queue_active = False
        self._active_tasks = 0
        self._processed_num = 0
        self._fail_num = 0
        self._total_num = 0
        # 种子级别的锁（防止并发处理同一种子）
        self._torrent_locks: Dict[str, threading.Lock] = {}
        self._locks_manager_lock = threading.Lock()
        # 批量更新订阅
        self._pending_subscribe_updates: List = []  # 待批量更新的任务
        self._subscribe_update_lock = threading.Lock()
        # 指标收集
        self._current_metrics = None  # 当前周期的指标
        # 启动整理任务
        self.__init()

    def __init(self):
        """
        启动文件整理线程
        """
        self._queue_active = True
        for i in range(settings.TRANSFER_THREADS):
            logger.info(f"启动文件整理线程 {i + 1} ...")
            thread = threading.Thread(target=self.__start_transfer,
                                      name=f"transfer-{i}",
                                      daemon=True)
            self._threads.append(thread)
            thread.start()

    def __stop(self):
        """
        停止文件整理进程
        """
        self._queue_active = False
        for thread in self._threads:
            thread.join()
        self._threads = []
        logger.info("文件整理线程已停止")

    def on_config_changed(self):
        self.__stop()
        self.__init()

    def __is_subtitle_file(self, fileitem: FileItem) -> bool:
        """
        判断是否为字幕文件
        """
        if not fileitem.extension:
            return False
        return True if f".{fileitem.extension.lower()}" in self._subtitle_exts else False

    def __is_audio_file(self, fileitem: FileItem) -> bool:
        """
        判断是否为音频文件
        """
        if not fileitem.extension:
            return False
        return True if f".{fileitem.extension.lower()}" in self._audio_exts else False

    def __is_media_file(self, fileitem: FileItem) -> bool:
        """
        判断是否为主要媒体文件
        """
        if fileitem.type == "dir":
            # 蓝光原盘判断
            return StorageChain().is_bluray_folder(fileitem)
        if not fileitem.extension:
            return False
        return True if f".{fileitem.extension.lower()}" in self._media_exts else False

    def __is_allowed_file(self, fileitem: FileItem) -> bool:
        """
        判断是否允许的扩展名
        """
        if not fileitem.extension:
            return False
        return True if f".{fileitem.extension.lower()}" in self._allowed_exts else False

    @staticmethod
    def __is_allow_filesize(fileitem: FileItem, min_filesize: int) -> bool:
        """
        判断是否满足最小文件大小
        """
        return True if not min_filesize or (fileitem.size or 0) > min_filesize * 1024 * 1024 else False

    def __default_callback(self, task: TransferTask,
                           transferinfo: TransferInfo, /) -> Tuple[bool, str]:
        """
        整理完成后处理
        """

        # 状态
        ret_status = True
        # 错误信息
        ret_message = ""

        def __notify():
            """
            完成时发送消息、刮削事件、移除任务等
            """
            # 更新文件数量
            transferinfo.file_count = self.jobview.count(task.mediainfo, task.meta.begin_season) or 1
            # 更新文件大小
            transferinfo.total_size = self.jobview.size(task.mediainfo,
                                                        task.meta.begin_season) or task.fileitem.size
            # 更新文件清单
            with job_lock:
                transferinfo.file_list_new = self._success_target_files.pop(transferinfo.target_diritem.path, [])

            # 发送通知，实时手动整理时不发
            if transferinfo.need_notify and (task.background or not task.manual):
                se_str = None
                if task.mediainfo.type == MediaType.TV:
                    season_episodes = self.jobview.season_episodes(task.mediainfo, task.meta.begin_season)
                    if season_episodes:
                        se_str = f"{task.meta.season} {StringUtils.format_ep(season_episodes)}"
                    else:
                        se_str = f"{task.meta.season}"
                # 发送入库成功消息
                self.send_transfer_message(meta=task.meta,
                                           mediainfo=task.mediainfo,
                                           transferinfo=transferinfo,
                                           season_episode=se_str,
                                           username=task.username)

            # 刮削事件
            if transferinfo.need_scrape and self.__is_media_file(task.fileitem):
                self.eventmanager.send_event(EventType.MetadataScrape, {
                    'meta': task.meta,
                    'mediainfo': task.mediainfo,
                    'fileitem': transferinfo.target_diritem,
                    'file_list': transferinfo.file_list_new,
                    'overwrite': False
                })

        transferhis = TransferHistoryOper()

        # 转移失败
        if not transferinfo.success:
            logger.warn(f"{task.fileitem.name} 入库失败：{transferinfo.message}")

            # 新增转移失败历史记录
            history = transferhis.add_fail(
                fileitem=task.fileitem,
                mode=transferinfo.transfer_type if transferinfo else '',
                downloader=task.downloader,
                download_hash=task.download_hash,
                meta=task.meta,
                mediainfo=task.mediainfo,
                transferinfo=transferinfo
            )

            # 整理失败事件
            if self.__is_media_file(task.fileitem):
                # 主要媒体文件整理失败事件
                self.eventmanager.send_event(EventType.TransferFailed, {
                    'fileitem': task.fileitem,
                    'meta': task.meta,
                    'mediainfo': task.mediainfo,
                    'transferinfo': transferinfo,
                    'downloader': task.downloader,
                    'download_hash': task.download_hash,
                    'transfer_history_id': history.id if history else None,
                })
            elif self.__is_subtitle_file(task.fileitem):
                # 字幕整理失败事件
                self.eventmanager.send_event(EventType.SubtitleTransferFailed, {
                    'fileitem': task.fileitem,
                    'meta': task.meta,
                    'mediainfo': task.mediainfo,
                    'transferinfo': transferinfo,
                    'downloader': task.downloader,
                    'download_hash': task.download_hash,
                    'transfer_history_id': history.id if history else None,
                })
            elif self.__is_audio_file(task.fileitem):
                # 音频文件整理失败事件
                self.eventmanager.send_event(EventType.AudioTransferFailed, {
                    'fileitem': task.fileitem,
                    'meta': task.meta,
                    'mediainfo': task.mediainfo,
                    'transferinfo': transferinfo,
                    'downloader': task.downloader,
                    'download_hash': task.download_hash,
                    'transfer_history_id': history.id if history else None,
                })

            # 发送失败消息
            self.post_message(Notification(
                mtype=NotificationType.Manual,
                title=f"{task.mediainfo.title_year} {task.meta.season_episode} 入库失败！",
                text=f"原因：{transferinfo.message or '未知'}",
                image=task.mediainfo.get_message_image(),
                username=task.username,
                link=settings.MP_DOMAIN('#/history')
            ))

            # 设置任务失败
            self.jobview.fail_task(task)

            # 返回失败
            ret_status = False
            ret_message = transferinfo.message

        else:
            # 转移成功
            logger.info(f"{task.fileitem.name} 入库成功：{transferinfo.target_diritem.path}")

            # 新增task转移成功历史记录
            history = transferhis.add_success(
                fileitem=task.fileitem,
                mode=transferinfo.transfer_type if transferinfo else '',
                downloader=task.downloader,
                download_hash=task.download_hash,
                meta=task.meta,
                mediainfo=task.mediainfo,
                transferinfo=transferinfo
            )

            # task整理完成事件
            if self.__is_media_file(task.fileitem):
                # 主要媒体文件整理完成事件
                self.eventmanager.send_event(EventType.TransferComplete, {
                    'fileitem': task.fileitem,
                    'meta': task.meta,
                    'mediainfo': task.mediainfo,
                    'transferinfo': transferinfo,
                    'downloader': task.downloader,
                    'download_hash': task.download_hash,
                    'transfer_history_id': history.id if history else None,
                })
            elif self.__is_subtitle_file(task.fileitem):
                # 字幕整理完成事件
                self.eventmanager.send_event(EventType.SubtitleTransferComplete, {
                    'fileitem': task.fileitem,
                    'meta': task.meta,
                    'mediainfo': task.mediainfo,
                    'transferinfo': transferinfo,
                    'downloader': task.downloader,
                    'download_hash': task.download_hash,
                    'transfer_history_id': history.id if history else None,
                })
            elif self.__is_audio_file(task.fileitem):
                # 音频文件整理完成事件
                self.eventmanager.send_event(EventType.AudioTransferComplete, {
                    'fileitem': task.fileitem,
                    'meta': task.meta,
                    'mediainfo': task.mediainfo,
                    'transferinfo': transferinfo,
                    'downloader': task.downloader,
                    'download_hash': task.download_hash,
                    'transfer_history_id': history.id if history else None,
                })

            # task登记转移成功文件清单
            target_dir_path = transferinfo.target_diritem.path
            target_files = transferinfo.file_list_new
            with job_lock:
                if self._success_target_files.get(target_dir_path):
                    self._success_target_files[target_dir_path].extend(target_files)
                else:
                    self._success_target_files[target_dir_path] = target_files

            # 设置任务成功
            self.jobview.finish_task(task)

        # 全部整理完成且有成功的任务时，发送消息和事件
        if self.jobview.is_finished(task):
            __notify()

        # 全部整理完成，设置完成的种子为已整理
        if self.jobview.is_done(task):
            # 查询作业中的所有任务
            tasks = self.jobview.all_tasks(task.mediainfo, task.meta.begin_season)
            processed_hashes = set()
            for t in tasks:
                if t.download_hash and t.download_hash not in processed_hashes:
                    # 检查该种子的所有任务（跨作业）是否都已完成
                    if self.jobview.is_torrent_done(t.download_hash):
                        # 仅当所有任务都成功时才通知下载器和标记状态
                        if self.jobview.is_torrent_success(t.download_hash):
                            # 完整性校验
                            is_complete, reason = self._verify_transfer_completeness(t.download_hash)
                            if not is_complete:
                                logger.warning(f"种子 {t.download_hash} 完整性校验失败: {reason}，跳过标记")
                                continue
                            
                            # 通知下载器
                            self.transfer_completed(hashs=t.download_hash, downloader=t.downloader)
                            # 标记本地状态
                            self._state_manager.mark_transferred(t.download_hash)
                            logger.info(f"种子 {t.download_hash} 全部任务成功且完整性校验通过，已标记完成")
                            
                            # 收集待批量更新的订阅（而非即时更新）
                            with self._subscribe_update_lock:
                                self._pending_subscribe_updates.append(t)
                            
                            # 采用上游：移动模式删除种子
                            if transferinfo.transfer_type in ["move"]:
                                if self.remove_torrents(t.download_hash, downloader=t.downloader):
                                    logger.info(f"移动模式删除种子成功：{t.download_hash}")
                        else:
                            logger.debug(f"种子 {t.download_hash} 有任务失败，不标记已完成，等待重试")
                        
                        # 记录已处理的哈希，避免重复处理
                        processed_hashes.add(t.download_hash)
            
            # 移动模式删除非种子任务的空目录
            if transferinfo.transfer_type in ["move"]:
                if self.jobview.is_success(task):
                    tasks = self.jobview.success_tasks(task.mediainfo, task.meta.begin_season)
                    for t in tasks:
                        if not t.download_hash and t.fileitem:
                            # 删除剩余空目录
                            StorageChain().delete_media_file(t.fileitem, delete_self=False)
            
            # 清理作业
            self.jobview.remove_job(task)

        return ret_status, ret_message


    @staticmethod
    def __update_subscribe_note(subscribe, downloads):
        """
        更新已下载信息到note字段
        """
        from app.db.subscribe_oper import SubscribeOper
        from app.schemas import MediaType

        # 查询现有Note
        if not downloads:
            return
        note = []
        if subscribe.note:
            note = subscribe.note or []
        for context in downloads:
            meta = context.meta_info
            mediainfo = context.media_info
            if subscribe.tmdbid and mediainfo.tmdb_id \
                    and mediainfo.tmdb_id != subscribe.tmdbid:
                continue
            if subscribe.doubanid and mediainfo.douban_id \
                    and mediainfo.douban_id != subscribe.doubanid:
                continue
            items = []
            if mediainfo.type == MediaType.TV:
                # 电视剧有集数，使用 episode_list
                items = meta.episode_list
            elif mediainfo.type == MediaType.MOVIE:
                # 电影只有一个条目，设置为 [1]
                items = [1]
            if not items:
                continue
            # 合并已下载的集数或电影项（去重）
            note = list(set(note).union(set(items)))
        # 更新订阅
        if note:
            SubscribeOper().update(subscribe.id, {
                "note": note
            })

    def put_to_queue(self, task: TransferTask) -> bool:
        """
        添加到待整理队列
        :param task: 任务信息
        :return: True表示任务已添加到队列，False表示任务无效或已存在（重复）
        """
        if not task:
            return False
        # 维护整理任务视图，如果任务已存在则不添加到队列
        if not self.__put_to_jobview(task):
            return False
        # 添加到队列
        self._queue.put(TransferQueue(
            task=task,
            callback=self.__default_callback
        ))
        return True

    def __put_to_jobview(self, task: TransferTask) -> bool:
        """
        添加到作业视图
        :return: True表示任务已添加，False表示任务无效或已存在（重复）
        """
        return self.jobview.add_task(task)

    def remove_from_queue(self, fileitem: FileItem):
        """
        从待整理队列移除
        """
        if not fileitem:
            return
        self.jobview.remove_task(fileitem)

    def __start_transfer(self):
        """
        处理队列
        """
        while not global_vars.is_system_stopped and self._queue_active:
            try:
                item: TransferQueue = self._queue.get(block=True, timeout=self._transfer_interval)
                if not item:
                    continue

                task = item.task
                if not task:
                    self._queue.task_done()
                    continue

                # 文件信息
                fileitem = task.fileitem

                with task_lock:
                    # 获取当前最新总数
                    current_total = self.jobview.total()
                    # 更新总数，取当前总数和当前已处理+运行中+队列中的最大值
                    self._total_num = max(self._total_num, current_total)

                    # 如果当前没有在运行的任务且处理数为0，说明是一个新序列的开始
                    if self._active_tasks == 0 and self._processed_num == 0:
                        logger.info("开始整理队列处理...")
                        # 启动进度
                        self._progress.start()
                        # 重置计数
                        self._processed_num = 0
                        self._fail_num = 0
                        __process_msg = f"开始整理队列处理，当前共 {self._total_num} 个文件 ..."
                        logger.info(__process_msg)
                        self._progress.update(value=0,
                                              text=__process_msg)
                    # 增加运行中的任务数
                    self._active_tasks += 1

                try:
                    # 更新进度
                    __process_msg = f"正在整理 {fileitem.name} ..."
                    logger.info(__process_msg)
                    with task_lock:
                        self._progress.update(
                            value=(self._processed_num / self._total_num * 100) if self._total_num else 0,
                            text=__process_msg)
                    # 整理
                    state, err_msg = self.__handle_transfer(task=task, callback=item.callback)

                    with task_lock:
                        if not state:
                            # 任务失败
                            self._fail_num += 1
                        # 更新进度
                        self._processed_num += 1
                        __process_msg = f"{fileitem.name} 整理完成"
                        logger.info(__process_msg)
                        self._progress.update(
                            value=(self._processed_num / self._total_num * 100) if self._total_num else 100,
                            text=__process_msg)
                except Exception as e:
                    logger.error(f"{fileitem.name} 整理任务处理出现错误：{e} - {traceback.format_exc()}")
                    with task_lock:
                        self._processed_num += 1
                        self._fail_num += 1
                finally:
                    self._queue.task_done()
                    with task_lock:
                        # 减少运行中的任务数
                        self._active_tasks -= 1
                        # 检查是否所有任务都已完成且队列为空
                        if self._active_tasks == 0 and self._queue.empty():
                            # 结束进度
                            __end_msg = f"整理队列处理完成，共整理 {self._processed_num} 个文件，失败 {self._fail_num} 个"
                            logger.info(__end_msg)
                            self._progress.update(value=100,
                                                  text=__end_msg)
                            self._progress.end()
                            # 重置计数
                            self._processed_num = 0
                            self._fail_num = 0

            except queue.Empty:
                # 即使队列空了，如果还有任务在运行，也不应该结束进度
                # 这部分逻辑已经在 finally 的 active_tasks == 0 中处理了
                continue
            except Exception as e:
                logger.error(f"整理队列处理出现错误：{e} - {traceback.format_exc()}")

    def __handle_transfer(self, task: TransferTask,
                          callback: Optional[Callable] = None) -> Optional[Tuple[bool, str]]:
        """
        处理整理任务
        """
        try:
            # 识别
            transferhis = TransferHistoryOper()
            mediainfo = task.mediainfo
            mediainfo_changed = False
            if not mediainfo:
                download_history = task.download_history
                # 下载用户
                if download_history:
                    task.username = download_history.username
                    # 识别媒体信息
                    if download_history.tmdbid or download_history.doubanid:
                        # 下载记录中已存在识别信息
                        mediainfo: Optional[MediaInfo] = self.recognize_media(mtype=MediaType(download_history.type),
                                                                              tmdbid=download_history.tmdbid,
                                                                              doubanid=download_history.doubanid,
                                                                              episode_group=download_history.episode_group)
                        if mediainfo:
                            # 更新自定义媒体类别
                            if download_history.media_category:
                                mediainfo.category = download_history.media_category
                else:
                    # 识别媒体信息
                    mediainfo = MediaChain().recognize_by_meta(task.meta)

                # 更新媒体图片
                if mediainfo:
                    self.obtain_images(mediainfo=mediainfo)

                if not mediainfo:
                    # 新增整理失败历史记录
                    his = transferhis.add_fail(
                        fileitem=task.fileitem,
                        mode=task.transfer_type,
                        meta=task.meta,
                        downloader=task.downloader,
                        download_hash=task.download_hash
                    )
                    self.post_message(Notification(
                        mtype=NotificationType.Manual,
                        title=f"{task.fileitem.name} 未识别到媒体信息，无法入库！",
                        text=f"回复：\n```\n/redo {his.id} [tmdbid]|[类型]\n```\n手动识别整理。",
                        username=task.username,
                        link=settings.MP_DOMAIN('#/history')
                    ))
                    # 任务失败，直接移除task
                    self.jobview.remove_task(task.fileitem)
                    return False, "未识别到媒体信息"

                mediainfo_changed = True

            # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
            if not settings.SCRAP_FOLLOW_TMDB:
                transfer_history = transferhis.get_by_type_tmdbid(tmdbid=mediainfo.tmdb_id,
                                                                  mtype=mediainfo.type.value)
                if transfer_history and mediainfo.title != transfer_history.title:
                    mediainfo.title = transfer_history.title
                    mediainfo_changed = True

            if mediainfo_changed:
                # 更新任务信息
                task.mediainfo = mediainfo
                # 更新队列任务
                curr_task = self.jobview.remove_task(task.fileitem)
                self.jobview.add_task(task, state=curr_task.state if curr_task else "waiting")

            # 获取集数据
            if task.mediainfo.type == MediaType.TV and not task.episodes_info:
                # 判断注意season为0的情况
                season_num = task.mediainfo.season
                if season_num is None and task.meta.season_seq:
                    if task.meta.season_seq.isdigit():
                        season_num = int(task.meta.season_seq)
                # 默认值1
                if season_num is None:
                    season_num = 1
                task.episodes_info = TmdbChain().tmdb_episodes(
                    tmdbid=task.mediainfo.tmdb_id,
                    season=season_num,
                    episode_group=task.mediainfo.episode_group
                )

            # 查询整理目标目录
            if not task.target_directory:
                if task.target_path:
                    # 指定目标路径，`手动整理`场景下使用，忽略源目录匹配，使用指定目录匹配
                    task.target_directory = DirectoryHelper().get_dir(media=task.mediainfo,
                                                                      dest_path=task.target_path,
                                                                      target_storage=task.target_storage)
                else:
                    # 启用源目录匹配时，根据源目录匹配下载目录，否则按源目录同盘优先原则，如无源目录，则根据媒体信息获取目标目录
                    task.target_directory = DirectoryHelper().get_dir(media=task.mediainfo,
                                                                      storage=task.fileitem.storage,
                                                                      src_path=Path(task.fileitem.path),
                                                                      target_storage=task.target_storage)
            if not task.target_storage and task.target_directory:
                task.target_storage = task.target_directory.library_storage

            # 正在处理
            self.jobview.running_task(task)

            # 广播事件，请示额外的源存储支持
            source_oper = None
            source_event_data = StorageOperSelectionEventData(
                storage=task.fileitem.storage,
            )
            source_event = eventmanager.send_event(ChainEventType.StorageOperSelection, source_event_data)
            # 使用事件返回的上下文数据
            if source_event and source_event.event_data:
                source_event_data: StorageOperSelectionEventData = source_event.event_data
                if source_event_data.storage_oper:
                    source_oper = source_event_data.storage_oper

            # 广播事件，请示额外的目标存储支持
            target_oper = None
            target_event_data = StorageOperSelectionEventData(
                storage=task.target_storage,
            )
            target_event = eventmanager.send_event(ChainEventType.StorageOperSelection, target_event_data)
            # 使用事件返回的上下文数据
            if target_event and target_event.event_data:
                target_event_data: StorageOperSelectionEventData = target_event.event_data
                if target_event_data.storage_oper:
                    target_oper = target_event_data.storage_oper

            # 执行整理
            transferinfo: TransferInfo = self.transfer(fileitem=task.fileitem,
                                                       meta=task.meta,
                                                       mediainfo=task.mediainfo,
                                                       target_directory=task.target_directory,
                                                       target_storage=task.target_storage,
                                                       target_path=task.target_path,
                                                       transfer_type=task.transfer_type,
                                                       episodes_info=task.episodes_info,
                                                       scrape=task.scrape,
                                                       library_type_folder=task.library_type_folder,
                                                       library_category_folder=task.library_category_folder,
                                                       source_oper=source_oper,
                                                       target_oper=target_oper)
            if not transferinfo:
                logger.error("文件整理模块运行失败")
                return False, "文件整理模块运行失败"

            # 回调，位置传参：任务、整理结果
            if callback:
                return callback(task, transferinfo)

            return transferinfo.success, transferinfo.message

        finally:
            # 移除已完成的任务
            self.jobview.try_remove_job(task)

    def get_queue_tasks(self) -> List[TransferJob]:
        """
        获取整理任务列表
        """
        return self.jobview.list_jobs()

    def recommend_name(self, meta: MetaBase, mediainfo: MediaInfo) -> Optional[str]:
        """
        获取重命名后的名称
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :return: 重命名后的名称（含目录）
        """
        return self.run_module("recommend_name", meta=meta, mediainfo=mediainfo)

    def get_torrent_files(self, downloader: str, download_hash: str) -> List[str]:
        """
        获取种子文件列表
        """
        try:
            return self.run_module("torrent_files", tid=download_hash, downloader=downloader)
        except Exception as err:
            logger.error(f"获取种子文件列表出错：{str(err)}")
            return []

    def _get_torrent_lock(self, hash_str: str) -> threading.Lock:
        """
        获取指定种子的锁（延迟创建）
        :param hash_str: 种子Hash
        :return: 种子专用的锁对象
        """
        with self._locks_manager_lock:
            if hash_str not in self._torrent_locks:
                self._torrent_locks[hash_str] = threading.Lock()
            return self._torrent_locks[hash_str]

    def _release_torrent_lock(self, hash_str: str):
        """
        清理已完成种子的锁（防止内存泄漏）
        :param hash_str: 种子Hash
        """
        with self._locks_manager_lock:
            self._torrent_locks.pop(hash_str, None)

    def _flush_subscribe_updates(self):
        """批量更新订阅"""
        with self._subscribe_update_lock:
            if not self._pending_subscribe_updates:
                return
            
            tasks = self._pending_subscribe_updates.copy()
            self._pending_subscribe_updates.clear()
        
        try:
            self._batch_update_subscribe_notes(tasks)
            if self._current_metrics:
                self._current_metrics.subscribe_updates = len(tasks)
            logger.info(f"批量更新了 {len(tasks)} 个任务的订阅")
        except Exception as e:
            logger.error(f"批量更新订阅失败: {e}")
    
    def _batch_update_subscribe_notes(self, tasks: List[TransferTask]):
        """批量更新多个任务的订阅"""
        from app.db.subscribe_oper import SubscribeOper
        from app.db.models.subscribe import Subscribe
        
        # 按 (tmdbid, doubanid) 分组
        by_media = {}
        for task in tasks:
            if not task.mediainfo or not task.meta:
                continue
            key = (task.mediainfo.tmdb_id, task.mediainfo.douban_id)
            if key not in by_media:
                by_media[key] = {
                    'mediainfo': task.mediainfo,
                    'contexts': []
                }
            by_media[key]['contexts'].append(task)
        
        if not by_media:
            return
        
        # 批量查询订阅
        tmdb_ids = [k[0] for k in by_media.keys() if k[0]]
        douban_ids = [k[1] for k in by_media.keys() if k[1] and not k[0]]
        
        subscribeoper = SubscribeOper()
        subscribes: list[Subscribe] = []
        
        if tmdb_ids:
            subscribes.extend(subscribeoper.list_by_tmdbids(tmdb_ids))
        if douban_ids:
            subscribes.extend(subscribeoper.list_by_doubanids(douban_ids))
        
        # 建立id到订阅的映射
        subscribe_map = {}
        for sub in subscribes:
            keys = []
            if sub.tmdbid:
                keys.append((sub.tmdbid, None))
            if sub.doubanid:
                keys.append((None, sub.doubanid))
            for k in keys:
                subscribe_map[k] = sub
        
        # 更新订阅
        for media_key, media_data in by_media.items():
            subscribe = subscribe_map.get(media_key)
            if not subscribe:
                continue
            
            # 汇总contexts
            contexts = []
            for task in media_data['contexts']:
                class Context:
                    def __init__(self, meta_info, media_info):
                        self.meta_info = meta_info
                        self.media_info = media_info
                contexts.append(Context(task.meta, task.mediainfo))
            
            try:
                self.__update_subscribe_note(subscribe, contexts)
            except Exception as e:
                logger.error(f"更新订阅 {tmdbid or doubanid} 失败: {e}")
    
    
    def _verify_transfer_completeness(self, download_hash: str) -> Tuple[bool, str]:
        """
        验证种子整理的完整性
        :param download_hash: 种子Hash
        :return: (是否完整, 原因)
        """
        import random
        
        # 获取转移历史记录（使用正确的方法名）
        histories = TransferHistoryOper().list_by_hash(download_hash)
        if not histories:
            return False, "没有找到转移历史记录"
        
        # 检查所有记录的状态（status是布尔类型，True=成功，False/0=失败）
        failed_count = sum(1 for h in histories if not h.status)
        if failed_count > 0:
            return False, f"有 {failed_count} 个文件转移失败"
        
        # 验证目标文件存在性（采样检查，避免性能问题）
        sample_size = min(5, len(histories))
        sampled = random.sample(histories, sample_size) if len(histories) > 0 else []
        
        for history in sampled:
            if history.dest:
                dest_path = Path(history.dest)
                if not dest_path.exists():
                    return False, f"目标文件不存在: {history.dest}"
        
        return True, f"验证通过（共 {len(histories)} 个文件，采样 {sample_size} 个）"

    def process(self) -> bool:
        """
        获取下载器中的种子列表，并执行整理
        """
        # 全局锁，避免定时服务重复
        with downloader_lock:
            # 获取下载器监控目录
            download_dirs = DirectoryHelper().get_download_dirs()

            # 如果没有下载器监控的目录则不处理
            if not any(dir_info.monitor_type == "downloader" and dir_info.storage == "local"
                       for dir_info in download_dirs):
                return True

            logger.info("开始整理下载器中已经完成下载的文件 ...")

            # 初始化本次周期的指标收集
            self._current_metrics = TransferMetrics()

            # 主动同步活跃种子状态（提升清理时效性）
            active_hashes = self._state_manager.get_active_download_hashes()
            if active_hashes:
                logger.debug(f"主动同步 {len(active_hashes)} 个活跃种子状态")
                try:
                    # 获取活跃种子的最新信息
                    active_torrents = self.list_torrents(hashs=list(active_hashes))
                    if active_torrents:
                        # 同步状态（只同步活跃的，速度快）
                        self._state_manager.sync_with_downloader(
                            [t.dict() for t in active_torrents]
                        )
                except Exception as e:
                    logger.warning(f"主动同步活跃种子状态失败: {e}")

            # 从下载器获取种子列表
            if torrents_list := self.list_torrents(status=TorrentStatus.TRANSFER):
                seen = set()
                existing_hashes = self.jobview.get_all_torrent_hashes()
                torrents = [
                    torrent
                    for torrent in torrents_list
                    if (h := torrent.hash) not in existing_hashes
                       # 排除多下载器返回的重复种子
                       and (h not in seen and (seen.add(h) or True))
                ]
            else:
                torrents = []

            if not torrents:
                logger.info("没有已完成下载但未整理的任务")
                return False

            logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")

            try:
                # 批量检查种子是否可以整理（性能优化）
                torrent_infos = {}
                for torrent in torrents:
                    torrent_infos[torrent.hash] = {
                        'progress': torrent.progress,
                        'state': torrent.state,
                        'hash': torrent.hash
                    }

                # 批量检查
                transfer_results = self._state_manager.batch_check_transferable(torrent_infos)

                # 统计检查结果
                transferable_count = sum(1 for can_transfer, _ in transfer_results.values() if can_transfer)
                blocked_reasons = {}
                for hash_str, (can_transfer, reason) in transfer_results.items():
                    if not can_transfer:
                        blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1

                logger.info(f"种子整理检查完成：{len(torrents)} 个种子，{transferable_count} 个可整理")
                if blocked_reasons:
                    reason_summary = ", ".join([f"{reason}({count}个)" for reason, count in blocked_reasons.items()])
                    logger.info(f"跳过原因统计：{reason_summary}")

                    # 对于下载未完成的种子，提供恢复建议
                    incomplete_torrents = [t for t in torrents
                                         if not transfer_results.get(t.hash, (True, ""))[0]
                                         and "下载未完成" in transfer_results.get(t.hash, ("", ""))[1]]

                    if incomplete_torrents and len(incomplete_torrents) <= 5:  # 只对少量种子提供建议
                        logger.debug(f"为 {len(incomplete_torrents)} 个未完成种子提供恢复建议:")
                        for torrent in incomplete_torrents[:3]:  # 最多显示3个
                            suggestions = self._state_manager.get_error_recovery_suggestions(
                                torrent.hash, torrent_infos.get(torrent.hash)
                            )
                            if suggestions:
                                logger.debug(f"  {torrent.title[:50]}... 建议: {'; '.join(suggestions[:2])}")  # 最多2个建议

                for torrent in torrents:
                    if global_vars.is_system_stopped:
                        break

                    # 获取种子锁（防止并发处理）
                    torrent_lock = self._get_torrent_lock(torrent.hash)
                    if not torrent_lock.acquire(blocking=False):
                        logger.debug(f"种子 {torrent.title} ({torrent.hash[:8]}...) 正在被其他任务处理，跳过")
                        # 记录锁竞争
                        if self._current_metrics:
                            self._current_metrics.lock_contentions += 1
                        continue

                    try:
                        # 检查是否可以整理
                        can_transfer, reason = transfer_results.get(torrent.hash, (False, "未知错误"))
                        if not can_transfer:
                            logger.debug(f"种子 {torrent.title} ({torrent.hash[:8]}...) 跳过整理，原因：{reason}")
                            continue

                        # 文件路径
                        file_path = torrent.path
                        if not file_path.exists():
                            logger.warn(f"文件不存在：{file_path}")
                            continue

                        # 检查是否为下载器监控目录中的文件
                        is_downloader_monitor = False
                        for dir_info in download_dirs:
                            if dir_info.monitor_type != "downloader":
                                continue
                            if not dir_info.download_path:
                                continue
                            if file_path.is_relative_to(Path(dir_info.download_path)):
                                is_downloader_monitor = True
                                break
                        if not is_downloader_monitor:
                            logger.debug(f"文件 {file_path} 不在下载器监控目录中，不通过下载器进行整理")
                            continue

                        # 查询下载记录识别情况
                        downloadhis: DownloadHistory = DownloadHistoryOper().get_by_hash(torrent.hash)
                        if downloadhis:
                            # 类型
                            try:
                                mtype = MediaType(downloadhis.type)
                            except ValueError:
                                mtype = MediaType.TV
                            # 识别媒体信息
                            mediainfo = self.recognize_media(mtype=mtype,
                                                             tmdbid=downloadhis.tmdbid,
                                                             doubanid=downloadhis.doubanid,
                                                             episode_group=downloadhis.episode_group)
                            if mediainfo:
                                # 补充图片
                                self.obtain_images(mediainfo)
                                # 更新自定义媒体类别
                                if downloadhis.media_category:
                                    mediainfo.category = downloadhis.media_category

                        else:
                            # 非MoviePilot下载的任务，按文件识别
                            mediainfo = None

                        # 获取种子文件列表
                        torrent_files = []
                        torrent_file_list = self.get_torrent_files(downloader=torrent.downloader, download_hash=torrent.hash)
                        if torrent_file_list:
                            for tfile in torrent_file_list:
                                if isinstance(tfile, dict):
                                    tname = tfile.get("name")
                                    if tname:
                                        torrent_files.append(str(tname))
                        
                        # 执行异步整理，匹配源目录
                        transfer_success, _ = self.do_transfer(
                            fileitem=FileItem(
                                storage="local",
                                path=file_path.as_posix() + ("/" if file_path.is_dir() else ""),
                                type="dir" if not file_path.is_file() else "file",
                                name=file_path.name,
                                size=file_path.stat().st_size,
                                extension=file_path.suffix.lstrip('.'),
                            ),
                            mediainfo=mediainfo,
                            downloader=torrent.downloader,
                            download_hash=torrent.hash,
                            included_files=torrent_files
                        )
                        
                        # 如果整理成功（包含跳过）且没有产生任务，说明该种子所有文件都已整理完毕
                        if transfer_success and self.jobview.is_torrent_done(torrent.hash):
                            self.transfer_completed(hashs=torrent.hash, downloader=torrent.downloader)
                            self._state_manager.mark_transferred(torrent.hash)
                            logger.info(f"种子 {torrent.title} 所有文件均已整理或无需整理，标记完成")
                            # 清理已完成种子的锁（防止内存泄漏）
                            self._release_torrent_lock(torrent.hash)
                        
                        # 更新处理统计
                        if self._current_metrics:
                            self._current_metrics.torrents_processed += 1

                    finally:
                        # 释放锁
                        torrent_lock.release()

            finally:
                torrents.clear()
                del torrents
                
                # 批量更新订阅
                self._flush_subscribe_updates()
                
                # 输出本次周期的指标
                if self._current_metrics:
                    import json
                    metrics = self._current_metrics.to_dict()
                    logger.info(f"整理周期统计: {json.dumps(metrics, ensure_ascii=False)}")

            return True

    def __get_trans_fileitems(
        self,
        fileitem: FileItem,
        predicate: Optional[Callable[[FileItem, bool], bool]],
        verify_file_exists: bool = True,
    ) -> List[Tuple[FileItem, bool]]:
        """
        获取待整理文件项列表

        :param fileitem: 源文件项
        :param predicate: 用于筛选目录或文件项
            该函数接收两个参数：

            - `file_item`: 需要判断的文件项（类型为 `FileItem`）
            - `is_bluray_dir`: 表示该项是否为蓝光原盘目录（布尔值）

            函数应返回 `True` 表示保留该项，`False` 表示过滤掉

            若 `predicate` 为 `None`，则默认保留所有项
        :param verify_file_exists: 验证目录或文件是否存在，默认值为 `True`
        """
        if global_vars.is_system_stopped:
            raise OperationInterrupted()

        storagechain = StorageChain()

        def __is_bluray_sub(_path: str) -> bool:
            """
            判断是否蓝光原盘目录内的子目录或文件
            """
            return True if re.search(r"BDMV[/\\]STREAM", _path, re.IGNORECASE) else False

        def __get_bluray_dir(_storage: str, _path: Path) -> Optional[FileItem]:
            """
            获取蓝光原盘BDMV目录的上级目录
            """
            for p in _path.parents:
                if p.name == "BDMV":
                    return storagechain.get_file_item(storage=_storage, path=p.parent)
            return None

        def _apply_predicate(file_item: FileItem, is_bluray_dir: bool) -> List[Tuple[FileItem, bool]]:
            if predicate is None or predicate(file_item, is_bluray_dir):
                return [(file_item, is_bluray_dir)]
            return []

        if verify_file_exists:
            latest_fileitem = storagechain.get_item(fileitem)
            if not latest_fileitem:
                logger.warn(f"目录或文件不存在：{fileitem.path}")
                return []
            # 确保从历史记录重新整理时 能获得最新的源文件大小、修改日期等
            fileitem = latest_fileitem

        # 是否蓝光原盘子目录或文件
        if __is_bluray_sub(fileitem.path):
            if bluray_dir := __get_bluray_dir(fileitem.storage, Path(fileitem.path)):
                # 返回该文件所在的原盘根目录
                return _apply_predicate(bluray_dir, True)

        # 单文件
        if fileitem.type == "file":
            return _apply_predicate(fileitem, False)

        # 是否蓝光原盘根目录
        sub_items = storagechain.list_files(fileitem, recursion=False) or []
        if storagechain.contains_bluray_subdirectories(sub_items):
            # 当前目录是原盘根目录，不需要递归
            return _apply_predicate(fileitem, True)

        # 不是原盘根目录 递归获取目录内需要整理的文件项列表
        return [
            item
            for sub_item in sub_items
            for item in (
                self.__get_trans_fileitems(
                    sub_item, predicate, verify_file_exists=False
                )
                if sub_item.type == "dir"
                else _apply_predicate(sub_item, False)
            )
        ]

    def do_transfer(self, fileitem: FileItem,
                    meta: MetaBase = None, mediainfo: MediaInfo = None,
                    target_directory: TransferDirectoryConf = None,
                    target_storage: Optional[str] = None, target_path: Path = None,
                    transfer_type: Optional[str] = None, scrape: Optional[bool] = None,
                    library_type_folder: Optional[bool] = None, library_category_folder: Optional[bool] = None,
                    season: Optional[int] = None, epformat: EpisodeFormat = None, min_filesize: Optional[int] = 0,
                    downloader: Optional[str] = None, download_hash: Optional[str] = None,
                    force: Optional[bool] = False, background: Optional[bool] = True,
                    manual: Optional[bool] = False, continue_callback: Callable = None,
                    included_files: List[str] = None) -> Tuple[bool, str]:
        """
        执行一个复杂目录的整理操作
        :param fileitem: 文件项
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param target_directory:  目标目录配置
        :param target_storage: 目标存储器
        :param target_path: 目标路径
        :param transfer_type: 整理类型
        :param scrape: 是否刮削元数据
        :param library_type_folder: 媒体库类型子目录
        :param library_category_folder: 媒体库类别子目录
        :param season: 季
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param downloader: 下载器
        :param download_hash: 下载记录hash
        :param force: 是否强制整理
        :param background: 是否后台运行
        :param manual: 是否手动整理
        :param continue_callback: 继续处理回调
        :param included_files: 种子包含的文件列表
        返回：成功标识，错误信息
        """
        # 是否全部成功
        all_success = True

        # 自定义格式
        formaterHandler = FormatParser(eformat=epformat.format,
                                       details=epformat.detail,
                                       part=epformat.part,
                                       offset=epformat.offset) if epformat else None

        # 整理屏蔽词
        transfer_exclude_words = SystemConfigOper().get(SystemConfigKey.TransferExcludeWords)
        # 汇总错误信息
        err_msgs: List[str] = []


        def _filter(file_item: FileItem, is_bluray_dir: bool) -> bool:
            """
            过滤文件项
            
            :return: True 表示保留，False 表示排除
            """
            if continue_callback and not continue_callback():
                raise OperationInterrupted()
            
            # 如果提供了 included_files，优先检查
            if included_files:
                item_path = Path(file_item.path)
                is_included = False
                for include_file in included_files:
                    include_path = Path(include_file)
                    # 比较路径后缀匹配
                    if str(item_path).endswith(include_file) or item_path.name == include_path.name:
                        is_included = True
                        break
                if not is_included:
                    logger.debug(f"文件 {file_item.path} 不在种子文件列表中，跳过整理")
                    return False
            
            # 有集自定义格式，过滤文件
            if formaterHandler and not formaterHandler.match(file_item.name):
                return False
            # 过滤后缀和大小（蓝光目录、附加文件不过滤）
            if (
                not is_bluray_dir
                and not self.__is_subtitle_file(file_item)
                and not self.__is_audio_file(file_item)
            ):
                if not self.__is_media_file(file_item):
                    return False
                if not self.__is_allow_filesize(file_item, min_filesize):
                    return False
            # 回收站及隐藏的文件不处理
            if (
                file_item.path.find("/@Recycle/") != -1
                or file_item.path.find("/#recycle/") != -1
                or file_item.path.find("/.") != -1
                or file_item.path.find("/@eaDir") != -1
            ):
                logger.debug(f"{file_item.path} 是回收站或隐藏的文件")
                return False
            # 整理屏蔽词不处理
            if self._is_blocked_by_exclude_words(file_item.path, transfer_exclude_words):
                return False
            return True

        try:
            # 获取经过筛选后的待整理文件项列表
            file_items = self.__get_trans_fileitems(fileitem, predicate=_filter)
        except OperationInterrupted:
            return False, f"{fileitem.name} 已取消"

        if not file_items:
            logger.warn(f"{fileitem.path} 没有找到可整理的媒体文件")
            return False, f"{fileitem.name} 没有找到可整理的媒体文件"


        logger.info(f"正在计划整理 {len(file_items)} 个文件...")

        # 整理所有文件
        transfer_tasks: List[TransferTask] = []
        try:
            for file_item, bluray_dir in file_items:
                if global_vars.is_system_stopped:
                    raise OperationInterrupted()
                if continue_callback and not continue_callback():
                    raise OperationInterrupted()
                file_path = Path(file_item.path)

                # 整理成功的不再处理
                if not force:
                    transferd = TransferHistoryOper().get_by_src(file_item.path, storage=file_item.storage)
                    if transferd:
                        if not transferd.status:
                            all_success = False
                        logger.info(f"{file_item.path} 已整理过，如需重新处理，请删除整理记录。")
                        err_msgs.append(f"{file_item.name} 已整理过")
                        continue

                # 提前获取下载历史，以便获取自定义识别词
                download_history = None
                downloadhis = DownloadHistoryOper()
                if download_hash:
                    # 先按hash查询
                    download_history = downloadhis.get_by_hash(download_hash)
                elif bluray_dir:
                    # 蓝光原盘，按目录名查询
                    download_history = downloadhis.get_by_path(file_path.as_posix())
                else:
                    # 按文件全路径查询
                    download_file = downloadhis.get_file_by_fullpath(file_path.as_posix())
                    if download_file:
                        download_history = downloadhis.get_by_hash(download_file.download_hash)

                if not meta:
                    subscribe_custom_words = None
                    if download_history and isinstance(download_history.note, dict):
                        # 使用source动态获取订阅
                        subscribe = SubscribeChain().get_subscribe_by_source(download_history.note.get("source"))
                        subscribe_custom_words = subscribe.custom_words.split(
                            "\n") if subscribe and subscribe.custom_words else None
                    # 文件元数据(优先使用订阅识别词)
                    file_meta = MetaInfoPath(file_path, custom_words=subscribe_custom_words)
                else:
                    file_meta = meta

                # 合并季
                if season is not None:
                    file_meta.begin_season = season

                if not file_meta:
                    all_success = False
                    logger.error(f"{file_path.name} 无法识别有效信息")
                    err_msgs.append(f"{file_path.name} 无法识别有效信息")
                    continue

                # 自定义识别
                if formaterHandler:
                    # 开始集、结束集、PART
                    begin_ep, end_ep, part = formaterHandler.split_episode(file_name=file_path.name,
                                                                           file_meta=file_meta)
                    if begin_ep is not None:
                        file_meta.begin_episode = begin_ep
                    if part is not None:
                        file_meta.part = part
                    if end_ep is not None:
                        file_meta.end_episode = end_ep

                # 获取下载Hash
                if download_history and (not downloader or not download_hash):
                    _downloader = download_history.downloader
                    _download_hash = download_history.download_hash
                else:
                    _downloader = downloader
                    _download_hash = download_hash

                # 后台整理
                transfer_task = TransferTask(
                    fileitem=file_item,
                    meta=file_meta,
                    mediainfo=mediainfo,
                    target_directory=target_directory,
                    target_storage=target_storage,
                    target_path=target_path,
                    transfer_type=transfer_type,
                    scrape=scrape,
                    library_type_folder=library_type_folder,
                    library_category_folder=library_category_folder,
                    downloader=_downloader,
                    download_hash=_download_hash,
                    download_history=download_history,
                    manual=manual,
                    background=background
                )
                if background:
                    if self.put_to_queue(task=transfer_task):
                        logger.info(f"{file_path.name} 已添加到整理队列")
                    else:
                        logger.debug(f"{file_path.name} 已在整理队列中，跳过")
                else:
                    # 加入列表
                    if self.__put_to_jobview(transfer_task):
                        transfer_tasks.append(transfer_task)
                    else:
                        logger.debug(f"{file_path.name} 已在整理列表中，跳过")
        except OperationInterrupted:
            return False, f"{fileitem.name} 已取消"
        finally:
            file_items.clear()
            del file_items

        # 实时整理
        if transfer_tasks:
            # 总数量
            total_num = len(transfer_tasks)
            # 已处理数量
            processed_num = 0
            # 失败数量
            fail_num = 0
            # 已完成文件
            finished_files = []

            # 启动进度
            progress = ProgressHelper(ProgressKey.FileTransfer)
            progress.start()
            __process_msg = f"开始整理，共 {total_num} 个文件 ..."
            logger.info(__process_msg)
            progress.update(value=0,
                            text=__process_msg)
            try:
                for transfer_task in transfer_tasks:
                    if global_vars.is_system_stopped:
                        break
                    if continue_callback and not continue_callback():
                        break
                    # 更新进度
                    __process_msg = f"正在整理 （{processed_num + fail_num + 1}/{total_num}）{transfer_task.fileitem.name} ..."
                    logger.info(__process_msg)
                    progress.update(value=(processed_num + fail_num) / total_num * 100,
                                    text=__process_msg,
                                    data={
                                        "current": Path(transfer_task.fileitem.path).as_posix(),
                                        "finished": finished_files,
                                    })
                    state, err_msg = self.__handle_transfer(
                        task=transfer_task,
                        callback=self.__default_callback
                    )
                    if not state:
                        all_success = False
                        logger.warn(f"{transfer_task.fileitem.name} {err_msg}")
                        err_msgs.append(f"{transfer_task.fileitem.name} {err_msg}")
                        fail_num += 1
                    else:
                        processed_num += 1
                    # 记录已完成
                    finished_files.append(Path(transfer_task.fileitem.path).as_posix())
            finally:
                transfer_tasks.clear()
                del transfer_tasks

            # 整理结束
            __end_msg = f"整理队列处理完成，共整理 {total_num} 个文件，失败 {fail_num} 个"
            logger.info(__end_msg)
            progress.update(value=100,
                            text=__end_msg,
                            data={})
            progress.end()

        error_msg = "、".join(err_msgs[:2]) + (f"，等{len(err_msgs)}个文件错误！" if len(err_msgs) > 2 else "")
        return all_success, error_msg

    def remote_transfer(self, arg_str: str, channel: MessageChannel,
                        userid: Union[str, int] = None, source: Optional[str] = None):
        """
        远程重新整理，参数 历史记录ID TMDBID|类型
        """

        def args_error():
            self.post_message(Notification(channel=channel, source=source,
                                           title="请输入正确的命令格式：/redo [id] [tmdbid/豆瓣id]|[类型]，"
                                                 "[id]整理记录编号", userid=userid))

        if not arg_str:
            args_error()
            return
        arg_strs = str(arg_str).split()
        if len(arg_strs) != 2:
            args_error()
            return
        # 历史记录ID
        logid = arg_strs[0]
        if not logid.isdigit():
            args_error()
            return
        # TMDBID/豆瓣ID
        id_strs = arg_strs[1].split('|')
        media_id = id_strs[0]
        if not logid.isdigit():
            args_error()
            return
        # 类型
        type_str = id_strs[1] if len(id_strs) > 1 else None
        if not type_str or type_str not in [MediaType.MOVIE.value, MediaType.TV.value]:
            args_error()
            return
        state, errmsg = self.__re_transfer(logid=int(logid),
                                           mtype=MediaType(type_str),
                                           mediaid=media_id)
        if not state:
            self.post_message(Notification(channel=channel, title="手动整理失败", source=source,
                                           text=errmsg, userid=userid, link=settings.MP_DOMAIN('#/history')))
            return

    def __re_transfer(self, logid: int, mtype: MediaType = None,
                      mediaid: Optional[str] = None) -> Tuple[bool, str]:
        """
        根据历史记录，重新识别整理，只支持简单条件
        :param logid: 历史记录ID
        :param mtype: 媒体类型
        :param mediaid: TMDB ID/豆瓣ID
        """
        # 查询历史记录
        history: TransferHistory = TransferHistoryOper().get(logid)
        if not history:
            logger.error(f"整理记录不存在，ID：{logid}")
            return False, "整理记录不存在"
        # 按源目录路径重新整理
        src_path = Path(history.src)
        if not src_path.exists():
            return False, f"源目录不存在：{src_path}"
        # 查询媒体信息
        if mtype and mediaid:
            mediainfo = self.recognize_media(mtype=mtype, tmdbid=int(mediaid) if str(mediaid).isdigit() else None,
                                             doubanid=mediaid, episode_group=history.episode_group)
            if mediainfo:
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)
        else:
            mediainfo = MediaChain().recognize_by_path(str(src_path), episode_group=history.episode_group)
        if not mediainfo:
            return False, f"未识别到媒体信息，类型：{mtype.value}，id：{mediaid}"
        # 重新执行整理
        logger.info(f"{src_path.name} 识别为：{mediainfo.title_year}")

        # 删除旧的已整理文件
        if history.dest_fileitem:
            # 解析目标文件对象
            dest_fileitem = FileItem(**history.dest_fileitem)
            StorageChain().delete_file(dest_fileitem)

        # 强制整理
        if history.src_fileitem:
            state, errmsg = self.do_transfer(fileitem=FileItem(**history.src_fileitem),
                                             mediainfo=mediainfo,
                                             download_hash=history.download_hash,
                                             force=True,
                                             background=False,
                                             manual=True)
            if not state:
                return False, errmsg

        return True, ""

    def manual_transfer(self,
                        fileitem: FileItem,
                        target_storage: Optional[str] = None,
                        target_path: Path = None,
                        tmdbid: Optional[int] = None,
                        doubanid: Optional[str] = None,
                        mtype: MediaType = None,
                        season: Optional[int] = None,
                        episode_group: Optional[str] = None,
                        transfer_type: Optional[str] = None,
                        epformat: EpisodeFormat = None,
                        min_filesize: Optional[int] = 0,
                        scrape: Optional[bool] = None,
                        library_type_folder: Optional[bool] = None,
                        library_category_folder: Optional[bool] = None,
                        force: Optional[bool] = False,
                        background: Optional[bool] = False) -> Tuple[bool, Union[str, list]]:
        """
        手动整理，支持复杂条件，带进度显示
        :param fileitem: 文件项
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :param season: 季度
        :param episode_group: 剧集组
        :param transfer_type: 整理类型
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按类型建立目录
        :param library_category_folder: 是否按类别建立目录
        :param force: 是否强制整理
        :param background: 是否后台运行
        """
        logger.info(f"手动整理：{fileitem.path} ...")
        if tmdbid or doubanid:
            # 有输入TMDBID时单个识别
            # 识别媒体信息
            mediainfo: MediaInfo = MediaChain().recognize_media(tmdbid=tmdbid, doubanid=doubanid,
                                                                mtype=mtype, episode_group=episode_group)
            if not mediainfo:
                return (False,
                        f"媒体信息识别失败，tmdbid：{tmdbid}，doubanid：{doubanid}，type: {mtype.value if mtype else None}")
            else:
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)

            # 开始整理
            state, errmsg = self.do_transfer(
                fileitem=fileitem,
                target_storage=target_storage,
                target_path=target_path,
                mediainfo=mediainfo,
                transfer_type=transfer_type,
                season=season,
                epformat=epformat,
                min_filesize=min_filesize,
                scrape=scrape,
                library_type_folder=library_type_folder,
                library_category_folder=library_category_folder,
                force=force,
                background=background,
                manual=True
            )
            if not state:
                return False, errmsg

            logger.info(f"{fileitem.path} 整理完成")
            return True, ""
        else:
            # 没有输入TMDBID时，按文件识别
            state, errmsg = self.do_transfer(fileitem=fileitem,
                                             target_storage=target_storage,
                                             target_path=target_path,
                                             transfer_type=transfer_type,
                                             season=season,
                                             epformat=epformat,
                                             min_filesize=min_filesize,
                                             scrape=scrape,
                                             library_type_folder=library_type_folder,
                                             library_category_folder=library_category_folder,
                                             force=force,
                                             background=background,
                                             manual=True)
            return state, errmsg

    def send_transfer_message(self, meta: MetaBase, mediainfo: MediaInfo,
                              transferinfo: TransferInfo, season_episode: Optional[str] = None,
                              username: Optional[str] = None):
        """
        发送入库成功的消息
        """
        self.post_message(
            Notification(
                mtype=NotificationType.Organize,
                ctype=ContentType.OrganizeSuccess,
                image=mediainfo.get_message_image(),
                username=username,
                link=settings.MP_DOMAIN('#/history')
            ),
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo,
            season_episode=season_episode,
            username=username
        )

    @staticmethod
    def _is_blocked_by_exclude_words(file_path: str, exclude_words: list) -> bool:
        """
        检查文件是否被整理屏蔽词阻止处理
        :param file_path: 文件路径
        :param exclude_words: 整理屏蔽词列表
        :return: 如果被屏蔽返回True，否则返回False
        """
        if not exclude_words:
            return False

        for keyword in exclude_words:
            if keyword and re.search(r"%s" % keyword, file_path, re.IGNORECASE):
                logger.warn(f"{file_path} 命中屏蔽词 {keyword}")
                return True
        return False
