"""可取消的索引重建，使用独立暂存数据库并原子发布。

活动数据库连接不跨线程；准备阶段使用捕获的路径和工作线程连接，在所有者处理前关闭。
"""

from __future__ import annotations

import copy
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from src.core.library_schema import register_connection
from src.core.mod_identity import (
    IdentityConflict,
    read_deployment_identity,
    read_source_mod_id,
    scan_deployment_identities,
    validate_mod_id,
)
from src.core.mod_intake import ModIntakeService, intake_identity
from src.core.mod_package import (
    InvalidManagedMetadataError,
    read_container_marker,
    read_sidecar,
    restore_preview_references,
    write_container_marker,
)
from src.core.mod_storage import ModStorage
from src.core.mod_variant_store import ModVariantStore
from src.core.preview_cache_index import capture_source_version
from src.models.deployment import DeploymentRecord, DeploymentState
from src.models.mod import Mod
from src.utils import fs
from src.utils.i18n import N_
from src.utils.i18n import t as tr
from src.utils.logger import logger


def publish_rebuild_projection(db, game_id):
    """仅合并仍等于基线的活动记录。

    调用方持有写事务，日志、当前预设、未修改记录和新事件更新的记录均保留。
    """
    columns = [row[1] for row in db.execute("PRAGMA main.table_info(mods)")]
    same = " AND ".join(f'm."{c}" IS b."{c}"' for c in columns)
    changed = " OR ".join(f's."{c}" IS NOT b."{c}"' for c in columns)
    db.execute("CREATE TEMP TABLE IF NOT EXISTS rebuild_accepted(mod_id TEXT PRIMARY KEY)")
    db.execute("DELETE FROM rebuild_accepted")
    db.execute(
        f"""INSERT INTO rebuild_accepted
        SELECT s.mod_id FROM rebuild_stage.rebuild_candidates c
        JOIN rebuild_stage.mods s ON s.mod_id=c.mod_id
        LEFT JOIN rebuild_stage.rebuild_baseline_mods b ON b.mod_id=s.mod_id
        LEFT JOIN main.mods m ON m.mod_id=s.mod_id
        WHERE s.game_id=?
          AND (s.node_id IS NULL OR EXISTS (SELECT 1 FROM main.nodes n
                                          WHERE n.id=s.node_id AND n.game_id=s.game_id))
          AND NOT EXISTS (SELECT 1 FROM main.mods occupied
                          WHERE occupied.mod_id<>s.mod_id AND occupied.game_id=s.game_id
                            AND occupied.node_id IS s.node_id
                            AND directory_key(occupied.folder_name)=directory_key(s.folder_name))
          AND ((b.mod_id IS NULL AND m.mod_id IS NULL)
            OR (b.mod_id IS NOT NULL AND m.mod_id IS NOT NULL AND {same}))""",
        (game_id,),
    )
    accepted = [row[0] for row in db.execute("SELECT mod_id FROM rebuild_accepted")]
    names = ",".join(f'"{c}"' for c in columns)
    updates = ",".join(f'"{c}"=excluded."{c}"' for c in columns if c != "mod_id")
    db.execute(f"""INSERT INTO main.mods({names})
        SELECT {",".join("s." + chr(34) + c + chr(34) for c in columns)}
        FROM rebuild_stage.mods s JOIN rebuild_accepted a ON a.mod_id=s.mod_id
        LEFT JOIN rebuild_stage.rebuild_baseline_mods b ON b.mod_id=s.mod_id
        WHERE b.mod_id IS NULL OR {changed}
        ON CONFLICT(mod_id) DO UPDATE SET {updates}""")
    db.execute("""INSERT INTO main.deployment_records(mod_id,state,actual_path)
        SELECT s.mod_id,s.state,s.actual_path FROM rebuild_stage.deployment_records s
        JOIN rebuild_accepted a ON a.mod_id=s.mod_id
        LEFT JOIN rebuild_stage.rebuild_baseline_deployment_records b ON b.mod_id=s.mod_id
        LEFT JOIN main.deployment_records d ON d.mod_id=s.mod_id
        WHERE ((b.mod_id IS NULL AND d.mod_id IS NULL)
           OR (b.mod_id IS NOT NULL AND d.mod_id IS NOT NULL
               AND b.state IS d.state AND b.actual_path IS d.actual_path))
          AND (b.mod_id IS NULL OR b.state IS NOT s.state OR b.actual_path IS NOT s.actual_path)
        ON CONFLICT(mod_id) DO UPDATE SET state=excluded.state,actual_path=excluded.actual_path""")
    db.execute("""INSERT INTO main.tag_definitions
        SELECT DISTINCT t.* FROM rebuild_stage.tag_definitions t
        JOIN rebuild_stage.mod_tags mt ON mt.tag_id=t.id
        JOIN rebuild_accepted a ON a.mod_id=mt.mod_id WHERE TRUE
        ON CONFLICT DO NOTHING""")
    db.execute("DELETE FROM main.mod_tags WHERE mod_id IN (SELECT mod_id FROM rebuild_accepted)")
    db.execute("""INSERT INTO main.mod_tags SELECT t.mod_id,current.id,t.position
                  FROM rebuild_stage.mod_tags t JOIN rebuild_accepted a ON a.mod_id=t.mod_id
                  JOIN rebuild_stage.tag_definitions previous ON previous.id=t.tag_id
                  JOIN main.tag_definitions current ON current.game_id=previous.game_id
                    AND current.normalized_name=previous.normalized_name""")
    return accepted


def _description(path):
    """描述可按统一规则降级，身份校验不能降级。"""
    try:
        return read_sidecar(path)
    except InvalidManagedMetadataError:
        return None


class _SyncRestore:
    """提交后的独立工作，不持有数据库连接，使用完整的首次对账流程。"""

    def __init__(self, old_sync, sync, entries, source_root, deploy_root):
        """保存待恢复的同步实例、成员列表及源部署监听根。"""
        self.old_sync, self.sync = old_sync, sync
        self.entries = tuple(entries)
        self.source_root, self.deploy_root = source_root, deploy_root

    def run(self, progress=lambda done, total: None):
        """停止旧同步并逐项核对部署身份后登记新同步，单项失败记日志并继续报告进度。"""
        if self.old_sync is not None:
            try:
                self.old_sync.stop()
            except Exception as error:
                logger.warning("重建后停止旧 INI 同步失败: %s", error)
        for i, (identity, body, deployed) in enumerate(self.entries):
            try:
                marker = read_deployment_identity(deployed)
                if marker is None or marker["mod_id"] != identity:
                    raise IdentityConflict(tr("INI 同步的部署身份不符：{deployed}", deployed=deployed))
                self.sync.register_mod(
                    identity, body, deployed, source_watch_root=self.source_root, deploy_watch_root=self.deploy_root
                )
            except Exception as error:
                # 索引已经提交，后续 INI 注册失败不改变发布结果。
                logger.warning("重建后登记 INI 同步失败 (%s): %s", identity, error)
            progress(i + 1, len(self.entries))


class RebuildSession:
    """隔离索引重建的扫描、暂存、事务发布和同步恢复状态。"""

    def __init__(self, manager, game_id):
        """在锁内独占管理器的重建会话，初始化失败时撤销活动会话登记。"""
        with manager._library_epoch_lock:
            if getattr(manager, "_active_rebuild_session", None) is not None:
                raise RuntimeError(tr("索引重建正在进行，请等待完成"))
            manager._active_rebuild_session = self
        try:
            self._initialize(manager, game_id)
        except BaseException:
            manager._active_rebuild_session = None
            raise

    def _initialize(self, manager, game_id):
        """采集游戏、分类、已索引身份和部署根，拒绝未完成事务或活动文件库操作。"""
        from src.core.mod_manager import RebuildReport

        self.mm, self.gid = manager, game_id
        self._initial_active_game = manager.current_game_id
        self.cancel = threading.Event()
        self.invalid = []
        self.enriched = []
        self.position = 0
        self.report = RebuildReport()
        self._temp = None
        self.stage = None
        self._stage_ready = False
        self._published = False
        self._sync_busy = False
        self._notified = False
        self._repaired_markers = []
        self._check_pending()
        if manager.library_probe_busy():
            raise RuntimeError(tr("文件库操作正在进行，请等待完成后重建"))
        db = manager._index._db()
        self._source_db_path = Path(manager._index._db_path)
        if db.in_transaction:
            raise RuntimeError(tr("索引事务尚未完成，暂不能重建"))
        self.version = (db.total_changes, db.execute("PRAGMA data_version").fetchone()[0])
        self.chars = copy.deepcopy(manager.get_characters(game_id))
        self.root = manager._get_library_root(game_id)
        self.paths = {c.id: manager._index.paths.node_path(c.id) for c in self.chars}
        rows = db.execute("SELECT mod_id,game_id FROM mods").fetchall()
        self.existing_ids = {r[0] for r in rows if r[1] == game_id}
        self.owners = {r[0]: r[1] for r in rows}
        self.registered_paths = {}
        for identity in self.existing_ids:
            body = manager._index.paths.body_path(identity)
            self.registered_paths[str(body).casefold()] = identity
            self.registered_paths[str(body.parent).casefold()] = identity
        deployed = db.execute(
            "SELECT d.actual_path FROM deployment_records d "
            "JOIN mods m ON m.mod_id=d.mod_id "
            "WHERE m.game_id=? AND d.actual_path<>?",
            (game_id, ""),
        ).fetchall()
        self.roots = [Path(r[0]).parent for r in deployed]
        game = manager._game_service.get_game(game_id)
        root = (
            manager._storage.get_deploy_dir()
            if game_id == manager.current_game_id
            else Path(game.mods_path)
            if game and game.mods_path
            else None
        )
        if root is not None:
            self.roots.append(root)
        self.deploy_root = root
        self.resource_dir = manager._res_dir(game_id)
        manager._release_rebuild_inflight_ids()
        self.context_stamp = manager.library_probe_stamp

    def _check_pending(self):
        """检查游戏是否有未完成预设应用，存在时拒绝开始或发布重建。"""
        if self.mm._preset_manager.has_pending_apply(self.gid):
            raise RuntimeError(tr("预设事务尚未完成，暂不能重建索引"))

    def check_cancel(self):
        """检查取消事件并抛出中断异常，使调用方保留原索引。"""
        if self.cancel.is_set():
            raise InterruptedError(tr("重建已取消，原索引已保留"))

    def prepare(self, progress=lambda phase, done, total: None):
        """在工作线程准备，不访问活动索引连接。"""
        from src.core.mod_manager import ModManager

        self.check_cancel()

        def characters(_gid):
            """返回重建开始时冻结的分类集合，并在访问前检查取消。"""
            self.check_cancel()
            return self.chars

        def character_path(cid, _gid, create=False):
            """按冻结的分类编号返回源路径，访问前检查取消。"""
            self.check_cancel()
            return self.paths[cid]

        adapter = SimpleNamespace(
            current_game_id=self.gid,
            get_characters=characters,
            _get_character_library=character_path,
            _get_library_root=lambda _gid: self.root,
            _import_cleanup_recovery=getattr(self.mm, "_import_cleanup_recovery", None),
            _storage=SimpleNamespace(
                scan_library_directory=lambda *args, **kwargs: ModStorage.scan_library_directory(None, *args, **kwargs)
            ),
        )
        self.adapter = adapter
        self._prepare_stage()
        progress(N_("发现文件"), 0, 0)
        descriptors = ModManager._discover_library_mods(
            adapter, self.gid, invalid_metadata=self.invalid, cancel_cb=self.check_cancel
        )
        self.discovered_paths = {item["path"] for item in descriptors}
        self.report.discovered_paths = []
        managed = []
        seen = set()
        for i, item in enumerate(descriptors):
            self.check_cancel()
            container = Path(item["path"]).parent
            marker = read_container_marker(container)
            if marker is None:
                if str(Path(item["path"])).casefold() in self.registered_paths:
                    self.invalid.append(f"Registered source requires marker recovery: {item['path']}")
                else:
                    self.report.discovered_paths.append(item["path"])
                continue
            try:
                os.lstat(fs.native_path(container / ".d3dxmeta" / "variants"))
            except FileNotFoundError:
                variants_present = False
            except OSError as error:
                # 无法确认存在性时保留原有身份凭据。
                variants_present = True
                self.invalid.append(
                    tr("无法检查变体元数据，原文件已保留：{value}：{error}", value=item["path"], error=error)
                )
            else:
                variants_present = True
                try:
                    ModVariantStore(item["path"]).list()
                except (OSError, ValueError) as error:
                    self.invalid.append(
                        tr("变体元数据损坏，原文件已保留：{value}：{error}", value=item["path"], error=error)
                    )
            expected_parent = self.paths.get(item.get("node_id"), self.root)
            if Path(item["path"]).parent.parent != expected_parent:
                self.invalid.append(f"Source lies below an unregistered node: {item['path']}")
                continue
            identity_marker = marker
            try:
                validate_mod_id(marker.get("mod_id"))
            except IdentityConflict:
                indexed_identity = self.registered_paths.get(str(Path(item["path"])).casefold())
                if indexed_identity:
                    identity_marker = dict(marker, mod_id=indexed_identity)
                elif variants_present:
                    self.invalid.append(
                        tr("含已保存变体的 Mod 身份无法确认，原文件及 ID 已保留：{value}", value=item["path"])
                    )
                    continue
            identity, imported_at = intake_identity("rediscovery", identity_marker, used=set(self.owners) | seen)
            if identity in seen or self.owners.get(identity, self.gid) != self.gid:
                raise IdentityConflict(tr("重建遇到重复身份：{value}", value=item["path"]))
            seen.add(identity)
            item["mod_id"] = identity
            item["imported_at"] = imported_at
            if marker.get("mod_id") != identity or marker.get("imported_at") != imported_at:
                marker_path = Path(item["path"]).parent / ".d3dxmod"
                original = marker_path.read_bytes()
                write_container_marker(
                    Path(item["path"]).parent,
                    Path(item["path"]).name,
                    marker["content_hash"],
                    mod_id=identity,
                    imported_at=imported_at,
                )
                self._repaired_markers.append((marker_path, original, marker_path.read_bytes()))
            item["sidecar"] = _description(item["path"])
            managed.append(item)
            if i % 32 == 0:
                progress(N_("校验身份"), i, len(descriptors))
        self.enriched = self.mm._hash_rebuild_descriptors(
            managed,
            progress_cb=lambda d, t: progress(N_("计算哈希"), d, t),
            unreadable=self.invalid,
            resource_dir=self.resource_dir,
            cancel_cb=self.check_cancel,
        )
        for item in self.enriched:
            self.check_cancel()
            marker_path = Path(item["path"]).parent / ".d3dxmod"
            marker = read_container_marker(marker_path.parent)
            if marker["content_hash"] != item["content_hash"]:
                original = marker_path.read_bytes()
                write_container_marker(
                    marker_path.parent,
                    Path(item["path"]).name,
                    item["content_hash"],
                    mod_id=item["mod_id"],
                    imported_at=item["imported_at"],
                )
                self._repaired_markers.append((marker_path, original, marker_path.read_bytes()))
        self.check_cancel()
        progress(N_("扫描部署"), 0, 0)
        self.locations = scan_deployment_identities(self.roots)
        self.check_cancel()
        progress(N_("准备暂存索引"), 0, 0)

    def validate(self, progress=lambda phase, done, total: None):
        """暂存完成后在工作线程重新核对文件输入。"""
        changed = set()
        for i, item in enumerate(self.enriched):
            self.check_cancel()
            try:
                matches = (
                    read_source_mod_id(item["path"]) == item["mod_id"]
                    and capture_source_version(item["path"]) == item["source_version"]
                    and _description(item["path"]) == item.get("sidecar")
                )
            except (OSError, IdentityConflict):
                matches = False
            if not matches:
                changed.add(item["mod_id"])
            progress(N_("提交前校验"), i + 1, len(self.enriched))
        latest_locations = scan_deployment_identities(self.roots)
        changed.update(
            identity
            for identity in set(latest_locations) | set(self.locations)
            if latest_locations.get(identity) != self.locations.get(identity)
        )
        self.check_cancel()
        # 所有者暂存已完成，工作线程只访问独立连接。
        stage = sqlite3.connect(self.stage_path)
        register_connection(stage)
        try:
            with stage:
                stage.executemany(
                    "DELETE FROM rebuild_candidates WHERE mod_id=?", ((identity,) for identity in changed)
                )
                columns = [row[1] for row in stage.execute("PRAGMA table_info(mods)")]
                same = " AND ".join(f's."{c}" IS b."{c}"' for c in columns)
                stage.execute(f"""DELETE FROM rebuild_candidates WHERE mod_id IN (
                    SELECT s.mod_id FROM mods s JOIN rebuild_baseline_mods b ON b.mod_id=s.mod_id
                    LEFT JOIN deployment_records d ON d.mod_id=s.mod_id
                    LEFT JOIN rebuild_baseline_deployment_records old ON old.mod_id=s.mod_id
                    WHERE {same} AND d.state IS old.state AND d.actual_path IS old.actual_path
                      AND s.mod_id NOT IN (SELECT mod_id FROM rebuild_preview_resets))""")
        finally:
            stage.close()

    def _prepare_stage(self):
        """采集独立基线，重建不清除持久关系。"""

        self.check_cancel()
        self._temp = tempfile.TemporaryDirectory(prefix="d3dx-rebuild-")
        self.stage_path = Path(self._temp.name) / "stage.db"
        source = sqlite3.connect(self._source_db_path.as_uri() + "?mode=ro", uri=True)
        try:
            target = sqlite3.connect(self.stage_path)
            try:
                source.backup(target, pages=128, progress=lambda *args: self.check_cancel())
                self.check_cancel()
                register_connection(target)
                target.set_progress_handler(lambda: int(self.cancel.is_set()), 1000)
                with target:
                    for table in ("mods", "deployment_records", "nodes"):
                        target.execute(f"CREATE TABLE rebuild_baseline_{table} AS SELECT * FROM {table}")
                    target.execute("CREATE UNIQUE INDEX rebuild_baseline_mod_id ON rebuild_baseline_mods(mod_id)")
                    target.execute(
                        "CREATE UNIQUE INDEX rebuild_baseline_deployment_id ON "
                        "rebuild_baseline_deployment_records(mod_id)"
                    )
                    target.execute("CREATE TABLE rebuild_candidates (mod_id TEXT PRIMARY KEY)")
                    target.execute("CREATE TABLE rebuild_preview_resets (mod_id TEXT PRIMARY KEY)")
                self.check_cancel()
            finally:
                target.close()
        except sqlite3.Error:
            self.check_cancel()
            raise
        finally:
            source.close()
        self._stage_ready = True

    def _open_stage(self):
        """在暂存数据库准备完成后建立独立连接与路径服务，生成仅用于暂存写入的代理。"""
        if not self._stage_ready:
            raise RuntimeError(tr("重建暂存数据库尚未准备完成"))
        self.stage = copy.copy(self.mm._index)
        self.stage._conn = sqlite3.connect(self.stage_path)
        self.stage._conn.row_factory = sqlite3.Row
        self.stage.game_id = self.gid
        self.stage._library_rows_cache = None
        register_connection(self.stage._conn)
        from src.core.library_paths import LibraryPaths

        self.stage.paths = LibraryPaths(
            self.stage._conn, self.mm._index.paths.roots, self.mm._index.paths.preview_roots
        )
        self.proxy = SimpleNamespace(_index=self.stage, _storage=self.mm._storage, current_game_id=self.gid)

    def stage_batch(self, size=32):
        """在短事务内批量写入重建成员及部署投影，更新统计并按数量或时间预算让出执行。"""
        self.check_cancel()
        if self.stage is None:
            self._open_stage()
        start = time.monotonic()
        with self.stage.transaction():
            for item in self.enriched[self.position : self.position + size]:
                self.check_cancel()
                identity = item["mod_id"]
                sc = item.get("sidecar") or {}
                location = self.locations.get(identity)
                node_id = item.get("node_id", "")
                mod = Mod(
                    mod_id=identity,
                    game_id=self.gid,
                    name=sc.get("name") or item["folder_name"],
                    folder_name=item["folder_name"],
                    path=item["path"],
                    content_hash=item["content_hash"],
                    node_id=node_id,
                    preview_images=self.mm._order_previews(item.get("preview_images", []), sc),
                    imported_at=item["imported_at"],
                )
                self.mm._apply_sidecar_info(mod, sc)
                mod.preview_images = restore_preview_references(
                    sc,
                    mod.preview_images,
                    container=Path(mod.path).parent,
                    preview_cache=Path(self.stage.paths.preview_roots[self.gid]) / identity,
                    node=self.stage.paths.node_path(node_id) if node_id else None,
                    ignore_selection=True,
                )
                local = sc.get("local") or {}
                if local.get("preview_selection_locked") or local.get("excluded_archive_previews"):
                    self.stage._db().execute("INSERT OR IGNORE INTO rebuild_preview_resets VALUES (?)", (identity,))
                if self.stage.get_mod(identity) is None:
                    if not self.stage.add_mod(mod, commit=False):
                        raise RuntimeError(f"Rebuild staging insert failed: {mod.name}")
                else:
                    if not self.stage.update_mod(mod, commit=False):
                        raise RuntimeError(f"Rebuild staging update failed: {mod.name}")
                self.stage.upsert_deployment(
                    DeploymentRecord(
                        mod_id=identity,
                        state=DeploymentState.ACTIVE
                        if location and location.active
                        else DeploymentState.DISABLED
                        if location
                        else DeploymentState.ABSENT,
                        actual_path=str(location.path) if location else "",
                    ),
                    commit=False,
                )
                self.stage._db().execute("INSERT OR IGNORE INTO rebuild_candidates VALUES (?)", (identity,))
                self.report.registered += 1
                self.report.classified += bool(node_id)
                self.report.unclassified += not bool(node_id)
                self.position += 1
                if time.monotonic() - start >= 0.008:
                    break
        return self.position == len(self.enriched)

    def commit(self, *, defer_sync=False):
        """核对重建完成状态后发布索引，刷新预设引用并组织活动成员的 INI 同步恢复。

        可将同步工作交给调用方延后执行；发布完成后返回包含无效元数据的重建报告。"""
        self.check_cancel()
        self._check_pending()
        if self.mm.library_probe_busy():
            raise RuntimeError(tr("文件库操作正在进行，原索引已保留"))
        self._assert_version()
        if self.position != len(self.enriched) or self.stage is None:
            raise RuntimeError(tr("重建暂存尚未完成"))
        self._publish()
        self._published = True
        self.mm._preset_manager.refresh_references(self.gid, self.published_ids)
        self.report.invalid_metadata.extend(self.invalid)
        self.sync_work = _SyncRestore(None, None, (), self.root, self.deploy_root)
        if self.gid == self.mm.current_game_id:
            from src.core.ini_sync import IniSyncManager

            entries = []
            for identity in self.published_ids:
                mod = self.mm._index.get_mod(identity)
                record = self.mm._index.get_deployment(identity)
                if mod is not None and record is not None and record.state == DeploymentState.ACTIVE:
                    entries.append((identity, mod.path, record.actual_path))
                elif self.mm._ini_sync is not None:
                    self.mm._ini_sync.unregister_mod(identity)
                    self.mm._ini_sync_refs.pop(identity, None)
            sync = self.mm._ini_sync
            if sync is None and entries:
                sync = IniSyncManager()
                sync.on_synced = self.mm._on_deployment_ini_synced
                self.mm._ini_sync = sync
            for identity, _, _ in entries:
                self.mm._ini_sync_refs[identity] = (identity,)
            self.sync_work = _SyncRestore(None, sync, entries, self.root, self.deploy_root)
            with self.mm._library_epoch_lock:
                self.mm._library_epoch += 1
                self.mm._library_mutations += 1
            self._sync_busy = True
        if not defer_sync:
            self.sync_work.run()
            self.finish()
        return self.report

    def finish(self):
        """提交后的工作线程结束后，由所有者发出通知。"""
        self._end_sync_busy()
        if not self._notified:
            self._notified = True
            self.mm._emit("index_rebuilt", game_id=self.gid)

    def _end_sync_busy(self):
        """结束重建后的同步修改占用，并在锁内更新资料库版本和活动计数。"""
        if self._sync_busy:
            with self.mm._library_epoch_lock:
                self.mm._library_epoch += 1
                self.mm._library_mutations -= 1
            self._sync_busy = False

    def _assert_version(self):
        # 逐对象基线比较保护新事件；切换到其他游戏时终止当前重建会话。
        """确认当前游戏与会话开始时一致，切换游戏时拒绝继续发布。"""
        if self.mm.current_game_id != self._initial_active_game:
            raise RuntimeError("Active game changed during rebuild")

    def _publish(self):
        """附加暂存数据库，在主索引事务中发布重建投影，并保证解除附加及修改计数。"""
        from src.core.preview_operations import reset_preview_selection

        db = self.mm._index._db()
        db.execute("ATTACH DATABASE ? AS rebuild_stage", (str(self.stage_path),))
        mutation_started = False
        try:
            with ExitStack() as metadata_changes, self.mm._index.transaction():
                # 获取写锁后再次检查，覆盖初次检查与锁获取之间的并发变化。
                self._assert_version()
                self._check_pending()
                with self.mm._library_epoch_lock:
                    self.mm._library_epoch += 1
                    self.mm._library_mutations += 1
                mutation_started = True
                self.published_ids = publish_rebuild_projection(db, self.gid)
                accepted = set(self.published_ids)
                metadata_changes.enter_context(
                    reset_preview_selection(
                        self.mm,
                        {item["mod_id"]: item.get("sidecar") for item in self.enriched if item["mod_id"] in accepted},
                    )
                )

        finally:
            try:
                db.execute("DETACH DATABASE rebuild_stage")
            finally:
                if mutation_started:
                    with self.mm._library_epoch_lock:
                        self.mm._library_epoch += 1
                        self.mm._library_mutations -= 1

    def close(self):
        """结束同步占用并关闭暂存资源；未发布时仅恢复仍与本次写入一致的修复标记。"""
        try:
            self._end_sync_busy()
            if not self._published:
                for marker, original, written in reversed(self._repaired_markers):
                    try:
                        current = marker.read_bytes()
                    except FileNotFoundError:
                        continue
                    if current == written:
                        ModIntakeService._restore_marker(marker.parent, original)
        finally:
            try:
                if self.stage is not None:
                    self.stage._conn.close()
                    self.stage = None
            finally:
                try:
                    if self._temp is not None:
                        self._temp.cleanup()
                        self._temp = None
                finally:
                    if getattr(self.mm, "_active_rebuild_session", None) is self:
                        self.mm._release_rebuild_inflight_ids()
                        self.mm._active_rebuild_session = None
