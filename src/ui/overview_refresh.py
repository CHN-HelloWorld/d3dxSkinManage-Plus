"""分批解码独立库记录，完成后再替换视图。"""

import time
from collections import OrderedDict

from PySide6.QtCore import QObject, Qt, QTimer

from src.utils.logger import logger
from src.utils.runtime_metrics import MetricSamples


class OverviewRefreshController(QObject):
    """按时间片读取分类 Mod 列表，以游戏和视图版本隔离过期结果，并支持有限目录预取。"""

    BUDGET_MS = 3
    YIELD_MS = 4

    def __init__(
        self,
        manager,
        *,
        current,
        apply,
        prepare_item=None,
        on_loading=None,
        on_error=None,
        on_progress=None,
        recursive=None,
        cache_levels=0,
        parent=None,
    ):
        """保存范围、结果和进度回调，初始化有界缓存、预取状态与精确定时器。"""
        super().__init__(parent)
        self.manager, self.current, self.apply = manager, current, apply
        self.prepare_item = prepare_item
        self.on_loading, self.on_error = on_loading, on_error
        self.on_progress = on_progress
        self.recursive = recursive
        self.cache_levels = max(0, cache_levels)
        self._cache = OrderedDict()
        self._prefetch_controller = None
        self._prefetch_targets = []
        self._prefetch_key = ("", "")
        self.active = False
        self.closing = False
        self._iterator = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._slice)
        self.metrics = dict(completed=0, discarded=0, slice_ms=MetricSamples())

    def schedule(self):
        """安排当前范围刷新；准备失败时取消任务并通知错误回调，保留现有视图。"""
        try:
            self._schedule_current()
        except Exception:
            logger.exception("Overview refresh preserved its previous view")
            self.cancel()
            if self.on_error is not None:
                self.on_error()

    def _schedule_current(self):
        """选择有效缓存或接续同范围预取，再建立新的刷新任务；非递归根分类直接返回空列表。"""
        key = self.current()
        if self.closing or not key[0]:
            self.cancel()
            return
        recursive = True if self.recursive is None else bool(self.recursive())
        cache_key = (*key[:2], recursive)
        stamp = self.manager.library_view_stamp
        cached = self._cache.get(cache_key)
        cache_valid = cached is not None and cached[0] == stamp and not self.manager.library_view_busy()
        prefetch = self._prefetch_controller
        prepared = None
        if (
            not cache_valid
            and not recursive
            and prefetch is not None
            and prefetch.active
            and prefetch._iterator is not None
            and prefetch._key[:2] == key[:2]
            and prefetch._stamp == stamp
        ):
            # 取消预取前转交所有权，目录进入可接续已有解码，仍按范围和版本校验。
            prepared = (prefetch._iterator, prefetch._mods, prefetch._ids, prefetch._stamp)
            prefetch._iterator = None
        self.cancel()
        self._key, self._recursive, self._cache_key = key, recursive, cache_key
        if prepared is not None:
            self._iterator, self._mods, self._ids, self._stamp = prepared
        if cache_valid:
            self._cache.move_to_end(self._cache_key)
            self.apply(*cached[1:])
            return
        if not self._recursive and not self._key[1]:
            # 根分类常驻，后代 Mod 数据按搜索请求加载，不阻塞普通导航。
            self.apply([], [])
            return
        self.active = True
        if self.on_loading is not None:
            self.on_loading(self._key[0])
        self._timer.start(0)

    def cancel(self):
        """关闭当前迭代器并停止时间片，清空预取目标且取消从属预取任务。"""
        self._timer.stop()
        if self._iterator is not None:
            self._iterator.close()
        self._iterator = None
        self.active = False
        self._mods = []
        self._prefetch_targets.clear()
        if self._prefetch_controller is not None:
            self._prefetch_controller.cancel()

    def prefetch(self, game_id, parent_ids):
        """在当前游戏空闲时对有限数量的分类安排非递归预取，重复目标合并。"""
        if not self.cache_levels or self.closing or self.active or game_id != self.current()[0]:
            return
        self._prefetch_targets = [(game_id, parent_id) for parent_id in dict.fromkeys(parent_ids) if parent_id][
            : self.cache_levels
        ]
        if self._prefetch_controller is None:
            self._prefetch_controller = OverviewRefreshController(
                self.manager,
                current=lambda: (
                    self._prefetch_key if not self.closing and self.current()[0] == self._prefetch_key[0] else ("", "")
                ),
                recursive=lambda: False,
                prepare_item=self.prepare_item,
                apply=self._accept_prefetch,
                on_error=self._start_prefetch,
                parent=self,
            )
            self._prefetch_controller.YIELD_MS = 8
            self._prefetch_controller.BUDGET_MS = 8
        self._start_prefetch()

    def _start_prefetch(self):
        """跳过已有有效缓存的目录，依次启动当前游戏的下一项目录预取。"""
        while self._prefetch_targets and not self.closing and not self.active:
            key = self._prefetch_targets.pop(0)
            if key[0] != self.current()[0]:
                self._prefetch_targets.clear()
                return
            cached = self._cache.get((*key, False))
            if cached is not None and cached[0] == self.manager.library_view_stamp:
                continue
            self._prefetch_key = key
            self._prefetch_controller.schedule()
            return

    def _accept_prefetch(self, direct, subtree):
        """将有效预取结果写入 LRU 缓存，淘汰超限目录并继续后续预取。"""
        if self.closing or self.current()[0] != self._prefetch_key[0]:
            return
        self._cache[(*self._prefetch_key, False)] = (self._prefetch_controller._stamp, direct, subtree)
        self._cache.move_to_end((*self._prefetch_key, False))
        while len(self._cache) > self.cache_levels:
            self._cache.popitem(last=False)
        self._start_prefetch()

    def _slice(self):
        """在时间预算内读取并准备 Mod，阶段之间核验视图范围与列表数据版本。

        事务忙碌时延后读取，过期时重启刷新；完成后区分直接子项和子树列表，一次性交付结果并按需缓存。"""
        start = time.perf_counter()
        try:
            if self.closing or not self.current()[0]:
                self.cancel()
                return
            # 首条记录到达前也可能切换游戏，继续等待前撤销旧视图任务。
            if self._key != self.current():
                self.metrics["discarded"] += 1
                self.schedule()
                return
            if self.manager.library_view_busy():
                self._timer.start(50)
                return
            if self._iterator is None:
                self._stamp = self.manager.library_view_stamp
                game_id, parent_id = self._key[:2]
                self._ids = (
                    self.manager.get_subtree_ids(parent_id, game_id)
                    if self._recursive and parent_id
                    else [parent_id]
                    if parent_id
                    else None
                )
                self._mods = []
                self._iterator = self.manager._index.iter_mods(game_id, self._ids)
            elif self._stamp != self.manager.library_view_stamp:
                self.metrics["discarded"] += 1
                self.schedule()
                return
            while True:
                mod = next(self._iterator)
                if self.prepare_item is not None:
                    self.prepare_item(mod)
                self._mods.append(mod)
                if (time.perf_counter() - start) * 1000 >= self.BUDGET_MS:
                    if self.on_progress is not None:
                        self.on_progress(len(self._mods))
                    # 时间片之间为输入和视频保留事件循环时间，避免连续零延迟任务阻塞绘制。
                    self._timer.start(self.YIELD_MS)
                    return
        except StopIteration:
            if (
                self._key != self.current()
                or self._stamp != self.manager.library_view_stamp
                or self.manager.library_view_busy()
            ):
                self.metrics["discarded"] += 1
                self.schedule()
                return
            parent_id = self._key[1]
            if self._ids is None:
                direct, subtree = [], self._mods
            else:
                buckets = {cid: [] for cid in self._ids}
                for mod in self._mods:
                    buckets[mod.node_id].append(mod)
                direct = list(buckets.get(parent_id, ()))
                subtree = [mod for cid in self._ids for mod in buckets[cid]]
            self._iterator = None
            self.active = False
            self.metrics["completed"] += 1
            if self.cache_levels:
                self._cache[self._cache_key] = (self._stamp, direct, subtree)
                self._cache.move_to_end(self._cache_key)
                while len(self._cache) > self.cache_levels:
                    self._cache.popitem(last=False)
            self.apply(direct, subtree)
        except Exception:
            logger.exception("Overview refresh preserved its previous view")
            self.cancel()
            if self.on_error is not None:
                self.on_error()
        finally:
            self.metrics["slice_ms"].append((time.perf_counter() - start) * 1000)

    def stop(self):
        """停止刷新与预取并清空缓存，拒绝后续调度。"""
        self.closing = True
        self.cancel()
        self._cache.clear()

    def resume(self):
        """解除关闭标记并立即安排当前范围刷新。"""
        self.closing = False
        self.schedule()
