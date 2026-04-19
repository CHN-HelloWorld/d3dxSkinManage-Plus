"""同步容器 INI 覆盖文件与已启用部署中的 INI 内容。

每对路径保存上次一致的内容摘要。两侧相同则不写入；仅一侧变化时同步到另一侧；
两侧都变化时以部署内容为准，以保存运行期间的修改。写入后更新共同摘要，
防止同步产生的事件再次触发反向写入。

监控全部已启用部署的 INI，必要时创建源覆盖文件。事件按路径防抖，
由定时线程执行文件操作，不访问 Qt 控件或线程绑定的数据库连接。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Iterable
from functools import wraps
from pathlib import Path

from src.core.mod_package import (
    override_path_for,
    resolve_ini_overrides_dir,
)
from src.core.mod_runtime_prefs import PREFS_LOCK
from src.utils.i18n import t as tr
from src.utils.logger import logger


def _configuration_io(method):
    """获取同步对锁之前先获取配置事务锁。"""

    @wraps(method)
    def wrapped(*args, **kwargs):
        """在共享偏好锁内调用被包装方法，串行化配置文件读写。"""
        with PREFS_LOCK:
            return method(*args, **kwargs)

    return wrapped


try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False


def _hash_file(path: Path) -> str | None:
    """文件内容 sha1（不存在返回 None）。用于对账三方内容异同。"""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return None


def _read_bytes(path: Path) -> bytes | None:
    """读取文件全部字节；任何文件访问错误均返回 None。"""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _has_key_bindings(path: Path) -> bool | None:
    """该 ini 是否含软件会展示的 ``key=`` 按键项；读取失败返回 None。"""
    data = _read_bytes(path)
    if data is None:
        return None
    try:
        from src.core.ini_keys import parse_ini_text

        text = data.decode("utf-8", errors="surrogateescape")
        return bool(parse_ini_text(text, str(path)))
    except Exception:
        return None


def _is_linked(path: Path) -> bool:
    """path 是否为链接（symlink 或 hardlink nlink>1）。判断是否须就地写以保链接。"""
    try:
        if path.is_symlink():
            return True
        if path.exists() and path.stat().st_nlink > 1:
            return True
    except OSError:
        return False
    return False


def _path_key(path: str | Path) -> str:
    """watchdog 路径索引键：绝对化 + 平台大小写规范化，但不 resolve 链接目标。"""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _atomic_write(path: Path, data: bytes) -> bool:
    """写入文件并保留符号链接或硬链接关系。

    链接文件就地截断写入，使另一端继续读取同一文件内容；普通文件使用原子替换。
    原子替换会改变硬链接所指向的文件身份，因此不能用于链接文件。失败时返回 False。
    """
    import os

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _is_linked(path):
            with open(path, "wb") as fh:  # 就地写入以保留文件身份和链接关系。
                fh.write(data)
            return True
        tmp = path.with_name(path.name + ".synctmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(str(tmp), str(path))
        return True
    except OSError as e:
        logger.warning("ini 同步写入失败 (%s): %s", path, e)
        return False


class SyncPair:
    """一对需同步的 ini：源 override 文件 ↔ 部署副本 ini。

    ``override_path`` 可能尚不存在（运行期回写会首次创建它）。
    ``deployed_path`` 在 Mod 启用期间总存在。
    ``last_hash`` = 上次对账后两侧一致的内容 hash（判断变更方向的基准）。
    """

    __slots__ = (
        "rel",
        "override_path",
        "deployed_path",
        "last_hash",
        "has_key_bindings",
    )

    def __init__(
        self,
        rel: str,
        override_path: Path,
        deployed_path: Path,
        last_hash: str | None,
        has_key_bindings: bool | None = None,
    ):
        """保存相对 INI 路径、覆盖与部署位置、上次摘要及按键识别状态。"""
        self.rel = rel
        self.override_path = override_path
        self.deployed_path = deployed_path
        self.last_hash = last_hash
        # 用于过滤 UI 通知：无按键 ini 从“无→无”变化时无需重建按键面板；同时保留
        # “有→无”状态，确保删除最后一个 key= 后仍会刷新并隐藏旧显示。
        self.has_key_bindings = has_key_bindings


class IniSyncManager:
    """管理所有已启用 Mod 的 ini 双向同步。

    生命周期：启用（部署完成）→ register_mod；禁用（删部署前）→ final_reconcile_mod
    + unregister_mod；应用退出 → stop。
    """

    def __init__(self, enable_watch: bool = True, debounce_seconds: float = 0.12, sweep_seconds: float = 3.0):
        """初始化同步配对、路径索引及监听引用，设置防抖和定期核对间隔。"""
        self._pairs: dict[str, list[SyncPair]] = {}  # mod_id → pairs
        self._lock = threading.RLock()
        # 修改监控可能等待 watchdog 的分发锁，而回调会获取同步对锁，
        # 因此监控操作必须在同步对锁之外执行。
        self._watch_lock = threading.RLock()
        self._observer = None
        self._watched: set[str] = set()  # 已 schedule 的目录（去重）
        self._watch_handles: dict[str, object] = {}  # 规范路径 → watchdog ObservedWatch
        self._watch_refcounts: dict[str, int] = {}  # 多个 deployment 共用目录时引用计数
        self._mod_watch_keys: dict[str, set[str]] = {}  # deployment id → 观察目录键
        # 按事件路径定位同步对，避免单个文件保存触发全部文件校验。
        self._path_index: dict[str, list[tuple[str, SyncPair]]] = {}
        # 短时间内的创建、修改和移动事件合并为一次局部对账。
        self._debounce_seconds = max(0.0, float(debounce_seconds))
        self._pending_paths: set[str] = set()
        self._debounce_timer: threading.Timer | None = None
        self._debounce_deadline = 0.0
        self._stopped = False
        # enable_watch 为 False 时只提供手动对账，不启动文件监控。
        self._enable_watch = enable_watch
        # 部署目录重建或移动可能使原有监控句柄失效，因此定期检查已登记文件。
        # 先比较大小和修改时间，仅在签名变化时计算内容摘要并对账。
        self._sweep_seconds = max(0.0, float(sweep_seconds))
        self._sweep_timer: threading.Timer | None = None
        self._sweep_sigs: dict[int, tuple] = {}  # id(pair) → 两侧 stat 签名
        # INI 原先或现在含按键声明时通知调用方。回调可能在工作线程执行，
        # 须通过信号转交界面更新，不能直接访问 Qt 控件或线程绑定的数据库连接。
        self.on_synced = None  # cb(mod_id): 相关按键显示可能变化（watchdog 线程）

    # 注册 / 注销

    @_configuration_io
    def register_mod(
        self,
        mod_id: str,
        body_path: str,
        deploy_target: str,
        source_watch_root: str | Path | None = None,
        deploy_watch_root: str | Path | None = None,
        *,
        prepared_pairs: list[dict] | None = None,
    ) -> int:
        """登记已部署 Mod 的 INI 同步关系，返回登记数量。

        body_path 用于定位容器覆盖文件，deploy_target 指定部署目录。
        source_watch_root 和 deploy_watch_root 指定共享监控根，多个 Mod 可复用监控句柄。
        观察公共根可避免容器内部句柄阻止上级目录移动，并在部署子目录重建后继续接收事件。
        同一路径重新登记时保留运行期基线并安排局部核对，避免源库事件先到而吞掉修改通知。
        """
        # 重复登记（预设恢复/部署切换）必须先撤掉上一轮目录引用，否则观察句柄会永久累积。
        with self._watch_lock:
            with self._lock:
                if self._stopped:
                    return 0
                previous_pairs = {
                    (_path_key(pair.override_path), _path_key(pair.deployed_path)): (
                        pair.last_hash,
                        pair.has_key_bindings,
                    )
                    for pair in self._pairs.get(mod_id, ())
                }
                removed = self._remove_registration_locked(mod_id)
            self._unschedule_watches(removed)

        deploy_dir = Path(deploy_target)
        if not deploy_dir.is_dir():
            return 0
        pairs: list[SyncPair] = []
        retained_paths: list[Path] = []
        ini_files = []
        if prepared_pairs is None:
            try:
                ini_files = sorted(deploy_dir.rglob("*.ini"))
            except OSError:
                pass
        else:
            # 更新线程已经解码、建立关联并计算摘要，原子提交后在界面线程安装基线时不重复读取 INI。
            overrides = resolve_ini_overrides_dir(body_path)
            if overrides is not None:
                for data in prepared_pairs:
                    relative = Path(data["rel"])
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError(tr("同步路径必须位于 Mod 内"))
                    pairs.append(
                        SyncPair(
                            relative.as_posix(),
                            overrides / relative,
                            deploy_dir / relative,
                            data["last_hash"],
                            data.get("has_key_bindings"),
                        )
                    )
        for ini in ini_files:
            # 部署 INI 使用相对于部署根目录的路径，与覆盖文件的相对路径保持一致。
            try:
                rel = ini.relative_to(deploy_dir).as_posix()
            except ValueError:
                continue
            if ".d3dxmeta" in rel.split("/"):
                continue  # 元数据不同步
            op = override_path_for(body_path, rel)
            if op is None:
                continue  # 非容器本体 → 无 override 能力，跳过
            if not ini.is_file():
                if not ini.is_symlink() or not op.is_file():
                    continue
                # 程序目录被剪切后，外部部署中的软链接仍指向旧位置。按当前容器内
                # 同一相对 INI 重新绑定；无链接权限时复用既有的硬链接/复制回退。
                from src.core.mod_storage import ModStorage

                ModStorage().link_ini(op, ini)
                if not ini.is_file():
                    continue
            previous = previous_pairs.get((_path_key(op), _path_key(ini)))
            if previous is not None:
                pairs.append(SyncPair(rel, op, ini, *previous))
                retained_paths.append(ini)
                continue
            # 跨进程没有上一轮的内存基线。已有 override 与独立部署副本不一致时，按
            # mtime 判断关闭期间改动方向；时间相同仍以部署侧为准，与运行期冲突规则一致。
            # 新部署正常已叠加 override，两侧相同，因此这里不会产生额外写盘。
            deploy_hash = _hash_file(ini)
            override_hash = _hash_file(op)
            if (
                override_hash is not None
                and deploy_hash is not None
                and override_hash != deploy_hash
                and not _is_linked(ini)
            ):
                try:
                    deploy_mtime = ini.stat().st_mtime_ns
                    override_mtime = op.stat().st_mtime_ns
                except OSError:
                    deploy_mtime = override_mtime = 0
                if override_mtime > deploy_mtime:
                    data = _read_bytes(op)
                    if data is not None and _atomic_write(ini, data):
                        deploy_hash = override_hash
                        logger.info("ini 冷启动对账: override -> deploy (%s)", rel)
                else:
                    data = _read_bytes(ini)
                    if data is not None and _atomic_write(op, data):
                        override_hash = deploy_hash
                        logger.info("ini 冷启动对账: deploy -> override (%s)", rel)
            # last_hash 必须取冷启动对账后的共同内容，后续事件才能正确判断方向。
            pairs.append(SyncPair(rel, op, ini, deploy_hash, _has_key_bindings(ini)))
        with self._watch_lock:
            with self._lock:
                if self._stopped:
                    return 0
                # 锁外发现文件期间可能出现并发注册，安装结果时须原子替换该注册。
                removed = self._remove_registration_locked(mod_id)
                if pairs:
                    self._pairs[mod_id] = pairs
                    for pair in pairs:
                        for path in (pair.override_path, pair.deployed_path):
                            self._path_index.setdefault(_path_key(path), []).append((mod_id, pair))
            self._unschedule_watches(removed)
            if not pairs:
                return 0
            watch_keys: set[str] = set()
            self._add_mod_watch_locked(
                watch_keys,
                Path(deploy_watch_root) if deploy_watch_root else deploy_dir,
            )
            ov_dir = resolve_ini_overrides_dir(body_path)
            if ov_dir is not None:
                ov_dir.mkdir(parents=True, exist_ok=True)
                # 直接观察覆盖目录可能阻止 Windows 移动其上级容器，
                # 因此优先共享源库根监控；独立调用至少观察容器根。
                source_root = Path(source_watch_root) if source_watch_root else ov_dir.parent.parent
                self._add_mod_watch_locked(watch_keys, source_root)
            if watch_keys:
                self._mod_watch_keys[mod_id] = watch_keys
        logger.info("ini 同步已登记: %s（%d 对）", mod_id, len(pairs))
        self._ensure_sweep()
        if self._enable_watch and retained_paths:
            # 重建监控句柄期间可能丢失原生事件，仅补充核对本次沿用基线的路径。
            self.schedule_reconcile_paths(retained_paths)
        return len(pairs)

    def snapshot_mod(self, mod_id: str) -> list[dict]:
        """保留已有同步基线，供文件事务回滚使用。"""
        with self._lock:
            return [
                {"rel": pair.rel, "last_hash": pair.last_hash, "has_key_bindings": pair.has_key_bindings}
                for pair in self._pairs.get(mod_id, ())
            ]

    def unregister_mod(self, mod_id: str) -> None:
        """注销 Mod 的所有同步对（禁用/删除时调；应先 final_reconcile_mod）。"""
        with self._watch_lock:
            with self._lock:
                removed = self._remove_registration_locked(mod_id)
            self._unschedule_watches(removed)

    def _remove_registration_locked(self, mod_id: str) -> list:
        """持有两把锁时移除登记；返回需要在 pair 锁外退订的观察句柄。"""
        removed = []
        # 仅更新当前部署的路径，避免每次注册都重建其他路径而产生平方级开销。
        for pair in self._pairs.pop(mod_id, ()):
            self._sweep_sigs.pop(id(pair), None)
            for path in (pair.override_path, pair.deployed_path):
                key = _path_key(path)
                refs = [ref for ref in self._path_index.get(key, ()) if ref[0] != mod_id]
                if refs:
                    self._path_index[key] = refs
                else:
                    self._path_index.pop(key, None)
        for key in self._mod_watch_keys.pop(mod_id, set()):
            remaining = self._watch_refcounts.get(key, 0) - 1
            if remaining > 0:
                self._watch_refcounts[key] = remaining
                continue
            self._watch_refcounts.pop(key, None)
            self._watched.discard(key)
            watch = self._watch_handles.pop(key, None)
            if watch is not None:
                removed.append(watch)
        return removed

    def _unschedule_watches(self, watches: list) -> None:
        """仅持有监控锁，排队回调仍可获取同步对锁。"""
        if self._observer is None:
            return
        for watch in watches:
            try:
                self._observer.unschedule(watch)
            except (KeyError, RuntimeError):
                # 被删除的观察根可能已让 emitter 自行退出；本地状态仍须完成释放。
                pass

    def _add_mod_watch_locked(self, keys: set[str], directory: Path) -> None:
        """为当前登记增加一个去重观察引用。仅持有 ``_watch_lock``。"""
        key = _path_key(directory)
        if key in keys:
            return
        ensured = self._ensure_watch(directory)
        if ensured is not None:
            keys.add(ensured)

    # 对账（核心）

    @_configuration_io
    def _reconcile_pair(self, pair: SyncPair) -> str | None:
        """对账单个同步对并返回同步方向，没有变化时返回 None。

        链接文件共享实际内容，仅在摘要变化时通知界面。独立副本根据共同基线判断
        修改方向，同步完成后更新两侧基线，避免文件监控事件再次触发反向写入。
        """
        hd = _hash_file(pair.deployed_path)
        if hd is None:
            return None  # 部署副本不在（已 undeploy/未部署）→ 不动
        # 链接已同步文件内容，此处仅根据变化通知界面。
        if _is_linked(pair.deployed_path):
            if hd != pair.last_hash:
                pair.last_hash = hd
                return "deploy->override"  # 仅用于触发 UI 通知；数据已由链接同步
            return None
        ho = _hash_file(pair.override_path)
        src_hash = ho if ho is not None else pair.last_hash

        if hd == src_hash:
            pair.last_hash = hd
            return None  # 已同步

        # 部署与源不一致 —— 判断谁变了（相对 last_hash）
        if ho is not None and ho != pair.last_hash and hd == pair.last_hash:
            # override 被外部改、部署未变 → 源→部署
            data = _read_bytes(pair.override_path)
            if data is not None and _atomic_write(pair.deployed_path, data):
                pair.last_hash = ho
                return "override->deploy"
            return None

        # 其余（部署变了 / 两侧都变冲突）→ 部署→源（运行期回写落 override；冲突部署优先）
        data = _read_bytes(pair.deployed_path)
        if data is not None and _atomic_write(pair.override_path, data):
            pair.last_hash = hd
            return "deploy->override"
        return None

    def reconcile_all(self) -> int:
        """对账所有已登记同步对，返回发生实际同步的对数（手动全量回退处理接口）。"""
        with self._lock:
            refs = [(mid, pair) for mid, pairs in self._pairs.items() for pair in pairs]
        return self._reconcile_refs(refs)

    def reconcile_paths(self, paths: Iterable[str | Path]) -> int:
        """只对账事件路径命中的同步对；同一 pair 被多个路径命中时仅执行一次。"""
        keys = {_path_key(path) for path in paths if path}
        if not keys:
            return 0
        with self._lock:
            refs: list[tuple[str, SyncPair]] = []
            seen: set[tuple[str, int]] = set()
            for key in keys:
                for mod_id, pair in self._path_index.get(key, []):
                    identity = (mod_id, id(pair))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    refs.append((mod_id, pair))
        return self._reconcile_refs(refs)

    def _reconcile_refs(self, refs: Iterable[tuple[str, SyncPair]]) -> int:
        """对账给定快照；只有按键相关 ini 变化才合并发送 UI 通知。"""
        n = 0
        key_changed_mods: set[str] = set()
        for mod_id, pair in refs:
            try:
                # 注销前快照中的对象不拥有后续注册，即使 Mod 和路径相同。
                with PREFS_LOCK, self._lock:
                    if not any(current is pair for current in self._pairs.get(mod_id, ())):
                        continue
                    direction = self._reconcile_pair(pair)
                    if direction:
                        n += 1
                        previous_has_keys = pair.has_key_bindings
                        current_has_keys = _has_key_bindings(pair.deployed_path)
                        if current_has_keys is not None:
                            pair.has_key_bindings = current_has_keys
                        if previous_has_keys is True or current_has_keys is True:
                            key_changed_mods.add(mod_id)
            except Exception as e:
                logger.warning("ini 对账失败 (%s): %s", pair.rel, e)
        for mid in key_changed_mods:
            self._notify_synced(mid)
        return n

    def reconcile_mod(self, mod_id: str) -> int:
        """移除部署前对账指定 Mod，保存尚未同步的运行期间修改。"""
        with self._lock:
            refs = [(mod_id, pair) for pair in self._pairs.get(mod_id, [])]
        return self._reconcile_refs(refs)

    def _notify_synced(self, mod_id: str) -> None:
        """通知调用方按键相关内容已变化。

        回调可能在工作线程执行，调用方须将界面操作投递到主线程。
        """
        cb = self.on_synced
        if cb is None:
            return
        try:
            cb(mod_id)
        except Exception as e:
            logger.warning("ini 同步通知回调抛错 (%s): %s", mod_id, e)
