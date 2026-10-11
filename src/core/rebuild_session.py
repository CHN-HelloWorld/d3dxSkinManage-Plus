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
