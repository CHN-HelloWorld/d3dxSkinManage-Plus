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
