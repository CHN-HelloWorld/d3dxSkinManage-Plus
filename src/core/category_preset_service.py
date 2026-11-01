"""按游戏规划分类预设，事务化发布并恢复未完成操作。

每次调用使用独立 SQLite 连接；工作线程只共享管理器的保护注册表和修改计数。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from src.core.category_preset import (
    MAX_FILE_BYTES,
    CategoryPresetCancelled,
    CategoryPresetDocument,
    CategoryPresetError,
    CategoryPresetNode,
    _validate_path,
)
from src.core.category_preset_journal import (
    StagingOwnership,
    anchor_errors,
    digest,
    directory_anchors,
    inventory,
    matches,
    published_errors,
    receipt,
    remove_inventory,
    rename_exclusive,
    rollback_actions,
    safe_path,
)
from src.core.library_paths import LibraryPaths
from src.core.library_schema import directory_key, register_connection
from src.services.category_preset_file import (
    copy_category_preset_image,
    read_category_preset,
    validate_category_image,
    write_category_preset,
)
from src.utils.constants import CATEGORY_MARKER_FILE, META_DIR
from src.utils.i18n import N_
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir, resolve_stored_path


@dataclass(frozen=True)
class CategoryPresetTarget:
    """保存分类预设操作所绑定的游戏身份、显示名称及资料库根目录。"""

    game_id: str
    name: str
    name_en: str
    root: Path


@dataclass(frozen=True)
class CategoryPresetRow:
    """记录单个分类的预览比较结果、图片补全决策与本地保留信息。"""

    path: str
    name: str
    name_en: str
    node_type: str
    status: str
    image_path: str = ""
    has_image: bool = False
    fill_image: bool = False
    detail: str = ""
    local_name: str = ""
    local_node_type: str = ""
    local_image_path: str = ""


@dataclass(frozen=True)
class ImportPlan:
    """保存分类导入的比较结果、暂存凭据和目标指纹，供执行前复核。"""

    target: CategoryPresetTarget
    source_name: str
    preset_name: str
    suggested_game_name: str
    rows: tuple[CategoryPresetRow, ...]
    new_count: int
    existing_count: int
    fill_count: int
    conflict_count: int
    new_image_count: int
    can_import: bool
    document: CategoryPresetDocument
    staging: Path
    fingerprint: tuple
    stage_entries: dict


@dataclass(frozen=True)
class ExportPlan:
    """保存分类导出的节点快照、图片暂存副本、源指纹和图片问题清单。"""

    target: CategoryPresetTarget
    rows: tuple[CategoryPresetRow, ...]
    include_images: bool
    omitted_images: tuple[str, ...]
    image_count: int
    image_bytes: int
    image_issues: object
    document: CategoryPresetDocument
    staging: Path
    fingerprint: tuple
    images: object
    image_sources: tuple
    stage_entries: dict


@dataclass(frozen=True)
class ImportResult:
    """汇总分类导入数量，并携带提交后尚待恢复或清理的路径。"""

    game_id: str
    new_count: int
    existing_count: int
    fill_count: int
    new_image_count: int
    recovery_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportResult:
    """记录分类预设导出的最终文件位置及节点和图片数量。"""

    game_id: str
    path: Path
    node_count: int
    image_count: int


def category_preset_filename(name: str) -> str:
    """校验预设名称及 Windows 文件名限制，统一补齐一个分类预设扩展名。"""
    if not isinstance(name, str) or not name or name != name.strip():
        raise CategoryPresetError(tr("请输入有效的预设名称，不能以空格结尾"))
    while name.lower().endswith(".d3dxcp"):
        name = name[:-7]
    if not name or "/" in name or "\\" in name:
        raise CategoryPresetError(tr("预设名称必须是单个文件名"))
    _validate_path(name)
    if len((name + ".d3dxcp").encode("utf-16-le")) // 2 > 255:
        raise CategoryPresetError(tr("预设文件名超过 Windows 长度限制"))
    return name + ".d3dxcp"


def _cancel(cancelled):
    """检查调用方取消请求，收到请求时抛出分类预设取消异常。"""
    if cancelled and cancelled():
        raise CategoryPresetCancelled(tr("分类预设操作已取消"))


def _notify(progress, phase, percent):
    """在调用方提供进度回调时发送当前阶段和完成百分比。"""
    if progress:
        progress(phase, percent)


def _signature(path):
    """读取安全路径的身份、大小和修改时间；对象缺失时返回 None。"""
    safe_path(path)
    try:
        info = Path(path).stat()
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _copy(source, target, cancelled=None, *, owner=None):
    """分块排他复制文件，并按需同步暂存所有权和内容摘要。

    复制中检查取消及容量上限，写入后刷新磁盘并核对来源签名；源变化或读取失败时抛出异常。"""
    source, target = safe_path(source), safe_path(target)
    before = _signature(source)
    total = 0
    copied_digest = hashlib.sha256()
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        if owner:
            owner.opened_file(target, outgoing)
        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
            _cancel(cancelled)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise CategoryPresetError(tr("文件超过 1 GiB 上限"))
            outgoing.write(chunk)
            copied_digest.update(chunk)
            if owner:
                owner.file_digest(target, copied_digest.hexdigest())
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if before != _signature(source):
        raise CategoryPresetError(tr("来源发生变化，请重新比较：{source}", source=source))


class CategoryPresetService:
    """组织分类预设的预览、事务发布、导出和按持久凭据恢复。"""

    def __init__(self, manager):
        """绑定资料库管理器并保存数据库路径，供每次操作建立独立连接。"""
        self.manager = manager
        self.db_path = Path(manager._index._db_path)

    @contextmanager
    def _connection(self):
        """提供注册过目录排序规则的独立 SQLite 连接，并在上下文结束时关闭。"""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        register_connection(conn)
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    def _games(self):
        # 直接读取配置，不使用绑定界面数据库连接的 LibraryPaths。
        """直接读取游戏配置条目，避免使用绑定所有者线程数据库的路径服务。"""
        return self.manager._game_service._store.get_games()

    def _target(self, game_id):
        """根据游戏编号解析并检查分类根目录，游戏已删除时拒绝操作。"""
        game = next((g for g in self._games() if g["id"] == game_id), None)
        if game is None:
            raise CategoryPresetError(tr("目标游戏已被删除，请重新选择"))
        root = safe_path(get_data_dir() / game["resource_dir"] / "mods")
        return CategoryPresetTarget(game_id, game["name"], game["name_en"], root)

    def _outside(self, path):
        """校验路径未穿过链接且位于全部资料库和部署目录之外，违规时抛出错误。"""
        path = safe_path(path)
        resolved = directory_key(str(path.resolve()))
        roots = []
        for game in self._games():
            roots.append(get_data_dir() / game["resource_dir"] / "mods")
            if game.get("mods_path"):
                roots.append(Path(resolve_stored_path(game["mods_path"])))
        runtime_deployment = self.manager._storage.get_deploy_dir()
        if runtime_deployment:
            roots.append(Path(runtime_deployment))
        for root in roots:
            root_key = directory_key(str(root.resolve())).rstrip("\\")
            if resolved == root_key or resolved.startswith(root_key + "\\"):
                raise CategoryPresetError(tr("预设文件和暂存位置必须在所有分类库及部署目录之外：{path}", path=path))
        return path

    def _new_stage(self, parent=None, *, prefix=".category-preset-"):
        """在安全目录中创建独立暂存目录，Windows 继承父目录访问权限。"""
        base = self._outside(self.db_path.parent if parent is None else parent)
        if os.name == "nt":
            # mkdtemp 的 0o700 会设置私有 ACL，移动发布后资源管理器仍可能无法访问。
            # 使用随机名称排他创建，并与手动创建分类一样继承正常目录权限。
            stage = base / (prefix + uuid.uuid4().hex)
            stage.mkdir()
            return stage
        return Path(tempfile.mkdtemp(prefix=prefix, dir=base))

    def _cleanup_owned(self, owner):
        """清理仍与所有权凭据匹配的暂存对象，记录并返回保留路径。"""
        errors = owner.cleanup()
        if errors:
            logger.error("暂存清理保留未知或已改变的路径：%s", "、".join(errors))
        return errors

    def _snapshot(self, conn, target, extra=()):
        """采集分类记录、Mod 占用路径和文件状态指纹，供预览与执行比较一致性。"""
        paths = LibraryPaths(conn, {target.game_id: target.root})
        records = [
            dict(row)
            for row in conn.execute("SELECT * FROM nodes WHERE game_id=? ORDER BY sort_order,id", (target.game_id,))
        ]
        nodes = {}
        for row in records:
            location = safe_path(paths.node_path(row["id"]))
            relative = location.relative_to(target.root).as_posix()
            row["path"] = relative
            row["absolute"] = str(location)
            nodes[directory_key(relative)] = row
        mod_rows = [
            tuple(row) for row in conn.execute("SELECT * FROM mods WHERE game_id=? ORDER BY mod_id", (target.game_id,))
        ]
        mod_paths = {
            directory_key(paths.container_path(row[0]).relative_to(target.root).as_posix()) for row in mod_rows
        }
        fs_paths = {target.root}
        for row in nodes.values():
            location = Path(row["absolute"])
            fs_paths.update((location, location / CATEGORY_MARKER_FILE))
        fs_paths.update(target.root / path for path in extra)
        states = tuple(
            (str(path), _signature(path), digest(path) if path.name == CATEGORY_MARKER_FILE and path.is_file() else "")
            for path in sorted(fs_paths, key=str)
        )
        fingerprint = (tuple(tuple(row.items()) for row in records), tuple(mod_rows), states)
        return nodes, mod_paths, fingerprint

    def _assert_target(self, target):
        """重新读取目标游戏配置，名称或根目录与计划不符时拒绝继续。"""
        current = self._target(target.game_id)
        if current != target:
            raise CategoryPresetError(tr("目标游戏或分类根目录发生变化，请重新比较"))

    def _image_path(self, paths, row):
        """解析节点缩略图引用并检查路径安全；无图片引用时返回空字符串。"""
        if not row["thumbnail"]:
            return ""
        return str(safe_path(paths.resolve_image(row["id"], json.loads(row["thumbnail"]))))

    def preview_import(self, source, game_id, *, cancelled=None, progress=None):
        """验证并复制来源预设，提取图片后生成与目标分类的比较计划。

        暂存文件均登记所有权；来源变化或准备失败时仅清理本次拥有的暂存对象。"""
        target = self._target(game_id)
        source = self._outside(source)
        original = read_category_preset(source, cancelled=cancelled, progress=progress)
        stage = self._new_stage()
        owner = StagingOwnership(stage)
        try:
            fixed = stage / "input.d3dxcp"
            _copy(source, fixed, cancelled, owner=owner)
            document = read_category_preset(fixed, cancelled=cancelled, progress=progress)
            if document.payload_sha256 != original.payload_sha256:
                raise CategoryPresetError(tr("来源文件在准备时发生变化，请重新选择"))
            image_dir = stage / "images"
            owner.mkdir(image_dir)
            for image in document.images:
                _cancel(cancelled)
                suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/bmp": ".bmp",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                }[image.media_type]
                image_path = image_dir / (image.image_id + suffix)
                copy_category_preset_image(document, image.image_id, image_path, cancelled=cancelled)
                owner.completed_file(image_path, image.image_id)
            owner.verify()
            return self._compare(document, target, source.name, stage, dict(owner.entries), cancelled, progress)
        except BaseException:
            self._cleanup_owned(owner)
            raise

    def _compare(self, document, target, source_name, stage, entries, cancelled, progress):
        """比较预设与本地分类及 Mod 占用，传播祖先路径冲突并生成导入数量和决策。"""
        self._assert_target(target)
        with self._connection() as conn:
            nodes, mods, fingerprint = self._snapshot(conn, target, (n.path for n in document.nodes))
            paths = LibraryPaths(conn, {target.game_id: target.root})
            rows = []
            images = {p.stem: str(p) for p in (stage / "images").iterdir()}
            for number, node in enumerate(document.nodes):
                _cancel(cancelled)
                key = directory_key(node.path)
                local = nodes.get(key)
                location = target.root / node.path
                detail, local_image = "", ""
                status = "existing" if local else "new"
                if key in mods or (not local and os.path.lexists(location)):
                    status = "conflict"
                    kind = tr("同名 Mod") if key in mods or location.is_dir() else tr("同名文件")
                    detail = tr("该位置已有{kind}，无法创建分类目录：{location}", kind=kind, location=location)
                elif local and not location.is_dir():
                    status, detail = "conflict", tr("已登记分类目录缺失或被文件占用：{location}", location=location)
                elif local:
                    if (local["name"], local["name_en"], local["node_type"]) != (
                        node.name,
                        node.name_en,
                        node.node_type,
                    ):
                        detail = tr("名称或类型不同，保留本地设置")
                    if local["thumbnail"]:
                        local_image = self._image_path(paths, local)
                        if not Path(local_image).is_file():
                            detail += tr("；本地图片缺失，保留原图片引用")
                fill = bool(local and not local["thumbnail"] and node.image_id and status == "existing")
                rows.append(
                    CategoryPresetRow(
                        node.path,
                        node.name,
                        node.name_en,
                        node.node_type,
                        status,
                        images.get(node.image_id, ""),
                        bool(node.image_id),
                        fill,
                        detail,
                        local["name"] if local else "",
                        local["node_type"] if local else "",
                        local_image,
                    )
                )
                _notify(progress, N_("比较分类"), (number + 1) * 100 // len(document.nodes))
            blocked = {directory_key(row.path): row.detail for row in rows if row.status == "conflict"}
            for index, row in enumerate(rows):
                ancestor = row.path.rpartition("/")[0]
                while ancestor:
                    if directory_key(ancestor) in blocked:
                        rows[index] = replace(
                            row, status="conflict", fill_image=False, detail=blocked[directory_key(ancestor)]
                        )
                        break
                    ancestor = ancestor.rpartition("/")[0]
        new = sum(r.status == "new" for r in rows)
        existing = sum(r.status == "existing" for r in rows)
        fill = sum(r.fill_image for r in rows)
        conflicts = sum(r.status == "conflict" for r in rows)
        return ImportPlan(
            target,
            source_name,
            document.name,
            document.suggested_game_name,
            tuple(rows),
            new,
            existing,
            fill,
            conflicts,
            sum(r.status == "new" and r.has_image for r in rows),
            bool((new or fill) and not conflicts),
            document,
            stage,
            fingerprint,
            entries,
        )

    def recompare(self, plan, *, cancelled=None, progress=None):
        """重新核验暂存输入后，按目标游戏当前状态刷新导入比较计划。"""
        document = self._verify_input(plan, cancelled, progress)
        return self._compare(
            document,
            self._target(plan.target.game_id),
            plan.source_name,
            plan.staging,
            plan.stage_entries,
            cancelled,
            progress,
        )

    def _verify_input(self, plan, cancelled, progress):
        """重新读取暂存预设并核对预览对象凭据，发现内容变化时要求重新选择文件。"""
        self._outside(plan.staging)
        document = read_category_preset(plan.document.source_path, cancelled=cancelled, progress=progress)
        if document != plan.document:
            raise CategoryPresetError(tr("暂存预设发生变化，请重新选择文件"))
        for relative, expected in plan.stage_entries.items():
            _cancel(cancelled)
            path = plan.staging if relative == "." else plan.staging / relative
            if path != plan.document.source_path and not matches(path, expected):
                raise CategoryPresetError(tr("暂存预览发生变化，请重新选择文件：{path}", path=path))
        return document

    @contextmanager
    def _mutation(self):
        """在操作前后推进资料库版本并维护活动修改计数，异常退出也恢复计数。"""
        with self.manager._library_epoch_lock:
            self.manager._library_epoch += 1
            self.manager._library_mutations += 1
        try:
            yield
        finally:
            with self.manager._library_epoch_lock:
                self.manager._library_epoch += 1
                self.manager._library_mutations -= 1

    def _marker(self, row):
        """将分类数据库行转换为可写入磁盘的分类标记结构。"""
        return {
            "schema": 2,
            "kind": "d3dxSkinManage-Plus.category",
            "id": row["id"],
            "name": row["name"],
            "name_en": row["name_en"],
            "node_type": row["node_type"],
            "sort_order": row["sort_order"],
            "thumbnail": json.loads(row["thumbnail"]) if row["thumbnail"] else None,
        }

    def _prepare(self, plan, conn, cancelled, owner, progress=None):
        """在受所有权凭据管理的暂存树中构造新增目录、图片及标记。

        返回文件发布动作与数据库新增、更新记录；替换已有标记时保留备份，并在返回前校验暂存树。"""
        _notify(progress, N_("准备目录"), 0)
        prepared = 0
        last_percent = 0
        total = max(1, plan.new_count + plan.fill_count)
        tree = owner.root
        nodes, _, _ = self._snapshot(conn, plan.target, (n.path for n in plan.document.nodes))
        rows = {directory_key(row.path): row for row in plan.rows}
        actions, inserts, updates = [], [], []
        next_sort = {}
        for row in nodes.values():
            parent = directory_key(row["path"].rpartition("/")[0])
            next_sort[parent] = max(next_sort.get(parent, 0), row["sort_order"] + 1)
        ordered = sorted(
            enumerate(plan.document.nodes), key=lambda pair: (pair[1].path.count("/"), pair[1].sort_order, pair[0])
        )
        new_roots = []
        for _, node in ordered:
            _cancel(cancelled)
            key = directory_key(node.path)
            preview = rows[key]
            parent = directory_key(node.path.rpartition("/")[0])
            is_new = preview.status == "new"
            if not is_new and not preview.fill_image:
                continue
            if is_new:
                row = {
                    "id": uuid.uuid4().hex,
                    "game_id": plan.target.game_id,
                    "parent_id": nodes[parent]["id"] if parent != "." else None,
                    "directory_name": node.path.rsplit("/", 1)[-1],
                    "name": node.name,
                    "name_en": node.name_en,
                    "node_type": node.node_type,
                    "sort_order": next_sort.get(parent, 0),
                    "thumbnail": "",
                    "path": node.path,
                }
                next_sort[parent] = row["sort_order"] + 1
                nodes[key] = row
                inserts.append(row)
                location = tree / node.path
                owner.mkdir(location, parents=True)
                if parent == "." or rows[parent].status != "new":
                    new_roots.append(node.path)
            else:
                row = dict(nodes[key])
                updates.append(row)
                location = tree / ("fill-" + row["id"])
                owner.mkdir(location)
            if node.image_id:
                meta = location / META_DIR
                owner.mkdir(meta)
                suffix = Path(preview.image_path).suffix
                image_name = "category-" + uuid.uuid4().hex + suffix
                image = meta / image_name
                copy_category_preset_image(plan.document, node.image_id, image, cancelled=cancelled)
                owner.completed_file(image, node.image_id)
                row["thumbnail"] = json.dumps({"scope": "node", "path": f"{META_DIR}/{image_name}"}, ensure_ascii=False)
                if not is_new:
                    final_meta = plan.target.root / node.path / META_DIR
                    safe_path(final_meta)
                    if final_meta.exists():
                        if not final_meta.is_dir():
                            raise CategoryPresetError(tr("目录图片位置被文件占用：{final_meta}", final_meta=final_meta))
                        actions.append(
                            {
                                "kind": "create",
                                "source": str(image),
                                "target": str(final_meta / image_name),
                                "after": owner.entries[owner.key(image)],
                            }
                        )
                    else:
                        actions.append(
                            {
                                "kind": "tree",
                                "source": str(meta),
                                "target": str(final_meta),
                                "entries": owner.subtree(meta),
                            }
                        )
            marker = safe_path(location / CATEGORY_MARKER_FILE)
            marker_bytes = json.dumps(self._marker(row), ensure_ascii=False).encode("utf-8")
            with marker.open("xb") as stream:
                owner.opened_file(marker, stream)
                stream.write(marker_bytes)
                owner.file_digest(marker, hashlib.sha256(marker_bytes).hexdigest())
                stream.flush()
                os.fsync(stream.fileno())
            if not is_new:
                final = plan.target.root / node.path / CATEGORY_MARKER_FILE
                if final.exists():
                    backup = location / "original-marker"
                    before = receipt(final)
                    _copy(final, backup, cancelled, owner=owner)
                    actions.append(
                        {
                            "kind": "replace",
                            "source": str(marker),
                            "target": str(final),
                            "before": before,
                            "after": owner.entries[owner.key(marker)],
                            "backup": str(backup),
                            "backup_receipt": owner.entries[owner.key(backup)],
                        }
                    )
                else:
                    actions.append(
                        {
                            "kind": "create",
                            "source": str(marker),
                            "target": str(final),
                            "after": owner.entries[owner.key(marker)],
                        }
                    )
            prepared += 1
            percent = min(99, prepared * 100 // total)
            if percent > last_percent:
                _notify(progress, N_("准备目录"), percent)
                last_percent = percent
        if not plan.target.root.exists():
            # 尚未打开的游戏可能没有资源根目录，将其作为一个受管目录树准备。
            root_stage = tree / ".root-publication"
            owner.mkdir(root_stage)
            for relative in new_roots:
                owner.move(tree / relative, root_stage / relative)
            root_target = plan.target.root
            while not root_target.parent.exists():
                wrapper = tree / (".parent-" + uuid.uuid4().hex)
                owner.mkdir(wrapper)
                owner.move(root_stage, wrapper / root_target.name)
                root_stage, root_target = wrapper, root_target.parent
            actions.insert(
                0,
                {
                    "kind": "tree",
                    "source": str(root_stage),
                    "target": str(root_target),
                    "entries": owner.subtree(root_stage),
                },
            )
        else:
            for relative in new_roots:
                actions.insert(
                    0,
                    {
                        "kind": "tree",
                        "source": str(tree / relative),
                        "target": str(plan.target.root / relative),
                        "entries": owner.subtree(tree / relative),
                    },
                )
        owner.verify()
        _notify(progress, N_("准备目录"), 100)
        return actions, inserts, updates

    def _publish(self, action):
        """核对动作的源及目标凭据后执行排他发布、比较替换或创建硬链接。"""
        source, target = safe_path(action["source"]), safe_path(action["target"])
        if action["kind"] == "tree":
            if inventory(source) != action["entries"]:
                raise CategoryPresetError(tr("暂存目录发生变化：{source}", source=source))
            rename_exclusive(source, target)
        elif action["kind"] == "replace":
            if not matches(target, action["before"]) or not matches(source, action["after"]):
                raise CategoryPresetError(tr("分类标记发生变化：{target}", target=target))
            os.replace(source, target)
        else:
            if not matches(source, action["after"]):
                raise CategoryPresetError(tr("暂存图片发生变化：{source}", source=source))
            os.link(source, target)

    def _commit_import(self, conn, operation_id, inserts, updates):
        """写入新增节点和空图片补全，将恢复日志标记为已提交并提交数据库事务。"""
        for row in inserts:
            conn.execute(
                """INSERT INTO nodes(id,game_id,parent_id,directory_name,name,name_en,node_type,sort_order,thumbnail)
                            VALUES(:id,:game_id,:parent_id,:directory_name,:name,:name_en,:node_type,:sort_order,:thumbnail)""",
                row,
            )
        for row in updates:
            conn.execute("UPDATE nodes SET thumbnail=? WHERE id=? AND thumbnail=?", (row["thumbnail"], row["id"], ""))
        conn.execute(
            "UPDATE category_import_journal SET phase='committed',updated_at=? WHERE operation_id=?",
            (datetime.now().isoformat(), operation_id),
        )
        conn.commit()

    def _recover_one(self, conn, journal, *, preserve_preview=()):
        """恢复单条分类导入日志，核对目录身份后撤销未提交发布并清理拥有的暂存内容。

        恢复失败时保留日志与路径保护；全部成功后删除日志并释放保护，可保留预览文件供重试。"""
        data = json.loads(journal["plan_json"])
        operation_id = journal["operation_id"]
        self.manager._library_protection.protect(operation_id, paths=data["protected_paths"])
        errors = []
        if journal["phase"] != "committed" and not data.get("rollback_done"):
            errors = anchor_errors(data.get("directory_anchors", {}))
            moved_actions = [
                action
                for action in data["actions"]
                if action["kind"] == "tree" and not os.path.lexists(action["source"])
            ]
            errors.extend(published_errors(moved_actions, directories_only=True))
            if errors:
                # 已移动的目录可能位于原位置旁边，身份恢复前保护其所在范围。
                self.manager._library_protection.protect(operation_id, paths=(Path(path).parent for path in errors))
            else:
                errors = rollback_actions(data["actions"])
                if not errors:
                    # 清理暂存目录前持久化实际文件结果，
                    # 避免后续恢复把已经撤回的输出误认为被外部移动。
                    data["rollback_done"] = True
                    conn.execute(
                        "UPDATE category_import_journal SET plan_json=?,updated_at=? WHERE operation_id=?",
                        (json.dumps(data, ensure_ascii=False), datetime.now().isoformat(), operation_id),
                    )
                    conn.commit()
        if not errors:
            entries = {key: value for key, value in data["stage_entries"].items() if key not in preserve_preview}
            errors = remove_inventory(journal["staging_path"], entries)
        if errors:
            logger.error("分类预设恢复未完成，保留记录及路径保护：%s", ", ".join(errors))
            return errors
        conn.execute("DELETE FROM category_import_journal WHERE operation_id=?", (operation_id,))
        conn.commit()
        self.manager._library_protection.release(operation_id)
        return []

    def recover_pending(self):
        """按创建顺序恢复未完成导入，汇总错误并保护凭据损坏的游戏根目录。"""
        errors = []
        with self._connection() as conn, self._mutation():
            journals = list(conn.execute("SELECT * FROM category_import_journal ORDER BY created_at"))
            for journal in journals:
                try:
                    errors.extend(self._recover_one(conn, journal))
                except (OSError, ValueError, sqlite3.Error) as error:
                    errors.append(f"{journal['staging_path']}：{error}")
                    # 恢复凭据损坏时仍禁止自动纳管。
                    self.manager._library_protection.protect(
                        journal["operation_id"], paths=(self._target(journal["game_id"]).root,)
                    )
        return tuple(errors)

    def execute_import(self, plan, *, cancelled=None, progress=None):
        """复核计划和输入，登记恢复日志后发布文件并提交分类记录。

        提交前失败或取消会按凭据回滚；数据库已提交后仅完成清理，清理错误通过结果中的恢复路径返回。"""
        self._assert_target(plan.target)
        self._verify_input(plan, cancelled, progress)
        if plan.conflict_count:
            raise CategoryPresetError(tr("目标存在路径冲突，请处理后重新比较"))
        result = ImportResult(
            plan.target.game_id, plan.new_count, plan.existing_count, plan.fill_count, plan.new_image_count
        )
        if not plan.can_import:
            return result
        operation_id = uuid.uuid4().hex
        registered = False
        publication = None
        with self._connection() as conn, self._mutation():
            try:
                if self._snapshot(conn, plan.target, (n.path for n in plan.document.nodes))[2] != plan.fingerprint:
                    raise CategoryPresetError(tr("目标分类状态发生变化，请重新比较"))
                anchors = directory_anchors(plan.target.root / row.path for row in plan.rows)
                publication = StagingOwnership(self._new_stage(plan.staging, prefix="publication-"))
                actions, inserts, updates = self._prepare(plan, conn, cancelled, publication, progress)
                protected = [
                    str(plan.target.root / row.path) for row in plan.rows if row.status == "new" or row.fill_image
                ]
                active_paths, _ = self.manager._library_protection.snapshot()
                if any(
                    directory_key(path) == p
                    or directory_key(path).startswith(p.rstrip("\\") + "\\")
                    or p.startswith(directory_key(path).rstrip("\\") + "\\")
                    for path in protected
                    for p in active_paths
                ):
                    raise CategoryPresetError(tr("目标目录有未完成操作，请稍后重试"))
                self.manager._library_protection.protect(operation_id, paths=protected)
                data = {
                    "target_root": str(plan.target.root),
                    "source_digest": plan.document.payload_sha256,
                    "source_path": str(plan.document.source_path),
                    "actions": actions,
                    "inserts": inserts,
                    "updates": updates,
                    "protected_paths": protected,
                    "stage_entries": {
                        **plan.stage_entries,
                        **{
                            publication.root.name + ("" if key == "." else "/" + key): value
                            for key, value in publication.entries.items()
                        },
                    },
                    "directory_anchors": anchors,
                }
                now = datetime.now().isoformat()
                conn.execute(
                    "INSERT INTO category_import_journal VALUES(?,?,?,?,?,?,?)",
                    (
                        operation_id,
                        plan.target.game_id,
                        "prepared",
                        json.dumps(data, ensure_ascii=False),
                        str(plan.staging),
                        now,
                        now,
                    ),
                )
                conn.commit()
                registered = True
                conn.execute("BEGIN IMMEDIATE")
                self._assert_target(plan.target)
                self._verify_input(plan, cancelled, progress)
                if self._snapshot(conn, plan.target, (n.path for n in plan.document.nodes))[2] != plan.fingerprint:
                    raise CategoryPresetError(tr("目标分类状态发生变化，请重新比较"))
                for index, action in enumerate(actions):
                    _cancel(cancelled)
                    self._assert_target(plan.target)
                    changed = anchor_errors(anchors, action["target"])
                    if index:
                        changed.extend(published_errors((actions[index - 1],)))
                    if changed:
                        raise CategoryPresetError(tr("目标目录或已发布内容发生变化：") + "、".join(changed))
                    self._publish(action)
                    _notify(progress, N_("导入分类"), (index + 1) * 100 // max(1, len(actions)))
                _cancel(cancelled)
                self._assert_target(plan.target)
                changed = anchor_errors(anchors) + published_errors(actions)
                if changed:
                    raise CategoryPresetError(tr("目标目录或已发布内容发生变化：") + "、".join(changed))
                _cancel(cancelled)
                self._commit_import(conn, operation_id, inserts, updates)
            except Exception:
                conn.rollback()
                if registered:
                    journal = conn.execute(
                        "SELECT * FROM category_import_journal WHERE operation_id=?", (operation_id,)
                    ).fetchone()
                    if journal and journal["phase"] == "committed":
                        # 数据库提交成功后，回调或驱动仍可能抛出异常。
                        return self._finish_commit(conn, operation_id, plan, result, progress)
                    errors = self._recover_one(conn, journal, preserve_preview=plan.stage_entries) if journal else []
                    if errors:
                        raise CategoryPresetError(
                            tr("导入未完成，以下路径待恢复并继续受保护：") + "、".join(errors)
                        ) from None
                else:
                    self.manager._library_protection.release(operation_id)
                    if publication is not None:
                        self._cleanup_owned(publication)
                raise
            # 数据库已经提交，取消请求或界面错误不能撤销该结果。
            return self._finish_commit(conn, operation_id, plan, result, progress)

    def _finish_commit(self, conn, operation_id, plan, result, progress):
        """通知导入提交结果并尝试清理恢复日志，将清理问题附加到成功结果中。"""
        try:
            _notify(progress, "已提交", 100)
        except Exception:
            logger.exception("分类预设已提交后的进度通知失败")
        try:
            journal = conn.execute(
                "SELECT * FROM category_import_journal WHERE operation_id=?", (operation_id,)
            ).fetchone()
            errors = self._recover_one(conn, journal, preserve_preview=plan.stage_entries)
        except Exception as error:
            errors = [f"{plan.staging}：{error}"]
            logger.exception("分类预设已提交，清理将在启动时重试")
        return replace(result, recovery_paths=tuple(errors))

    def preview_export(self, game_id, *, include_images=True, omitted_images=(), cancelled=None, progress=None):
        """采集分类快照并准备去重图片副本，保留图片问题供调用方决定是否省略。"""
        target = self._target(game_id)
        stage = self._new_stage()
        owner = StagingOwnership(stage)
        try:
            with self._connection() as conn:
                nodes, _, fingerprint = self._snapshot(conn, target)
                if not nodes:
                    raise CategoryPresetError(tr("该游戏没有可导出的分类目录"))
                paths = LibraryPaths(conn, {game_id: target.root})
                rows, document_nodes, images, issues, sources = [], [], {}, {}, []
                image_bytes = 0
                for row in sorted(nodes.values(), key=lambda r: (r["path"].count("/"), r["sort_order"], r["path"])):
                    _cancel(cancelled)
                    image_path, image_id = "", ""
                    if include_images and row["thumbnail"] and row["path"] not in omitted_images:
                        try:
                            image_path = self._image_path(paths, row)
                            signature = _signature(image_path)
                            image_id, _, size = validate_category_image(image_path, cancelled=cancelled)
                            if image_id not in images:
                                fixed = stage / (image_id + Path(image_path).suffix)
                                _copy(image_path, fixed, cancelled, owner=owner)
                                if validate_category_image(fixed, cancelled=cancelled)[0] != image_id:
                                    raise CategoryPresetError(tr("图片在准备时发生变化"))
                                images[image_id] = fixed
                                image_bytes += size
                            if _signature(image_path) != signature:
                                raise CategoryPresetError(tr("图片在准备时发生变化"))
                            sources.append((image_path, signature, image_id))
                        except (OSError, ValueError) as error:
                            image_id = ""
                            issues[row["path"]] = str(error)
                    rows.append(
                        CategoryPresetRow(
                            row["path"],
                            row["name"],
                            row["name_en"],
                            row["node_type"],
                            "existing",
                            image_path,
                            bool(image_id),
                            False,
                            issues.get(row["path"], ""),
                            row["name"],
                            row["node_type"],
                            image_path,
                        )
                    )
                    document_nodes.append(
                        CategoryPresetNode(
                            row["path"], row["name"], row["node_type"], row["name_en"], row["sort_order"], image_id
                        )
                    )
                self._assert_target(target)
                if self._snapshot(conn, target)[2] != fingerprint:
                    raise CategoryPresetError(tr("源分类发生变化，请重新预览"))
            document = CategoryPresetDocument(target.name, tuple(document_nodes), target.name, target.name_en)
            owner.verify()
            _notify(progress, "准备完成", 100)
            return ExportPlan(
                target,
                tuple(rows),
                include_images,
                tuple(omitted_images),
                len(images),
                image_bytes,
                MappingProxyType(issues),
                document,
                stage,
                fingerprint,
                MappingProxyType(images),
                tuple(sources),
                dict(owner.entries),
            )
        except BaseException:
            self._cleanup_owned(owner)
            raise

    def execute_export(self, plan, destination, name, *, cancelled=None, progress=None):
        """复核分类和图片来源后生成预设文件，再以排他重命名发布到可用文件名。

        发布前检查取消，成功后清理暂存计划；仅删除凭据仍匹配的未发布临时文件。"""
        filename = category_preset_filename(name)
        directory = self._outside(destination)
        if not directory.is_dir():
            raise CategoryPresetError(tr("请选择存在的保存目录"))
        if plan.image_issues:
            raise CategoryPresetError(tr("请先明确取消缺失或损坏的目录图片，再重新预览"))
        self._assert_target(plan.target)
        self._outside(plan.staging)
        temporary = directory / (".category-preset-" + uuid.uuid4().hex + ".d3dxcp")
        owned = None
        try:
            with self._connection() as conn:
                if self._snapshot(conn, plan.target)[2] != plan.fingerprint:
                    raise CategoryPresetError(tr("源分类发生变化，请重新预览"))
                for path, signature, sha in plan.image_sources:
                    _cancel(cancelled)
                    if _signature(path) != signature or digest(path) != sha:
                        raise CategoryPresetError(tr("源图片发生变化，请重新预览：{path}", path=path))
                written = write_category_preset(
                    temporary,
                    replace(plan.document, name=filename[:-7]),
                    plan.images,
                    cancelled=cancelled,
                    progress=progress,
                )
                owned = receipt(temporary)
                # 最终发布前重新读取暂存文件的实际内容。
                if read_category_preset(temporary, cancelled=cancelled, progress=progress) != written:
                    raise CategoryPresetError(tr("导出临时文件发生变化"))
                self._assert_target(plan.target)
                if self._snapshot(conn, plan.target)[2] != plan.fingerprint:
                    raise CategoryPresetError(tr("源分类发生变化，请重新预览"))
                for path, signature, sha in plan.image_sources:
                    if _signature(path) != signature or digest(path) != sha:
                        raise CategoryPresetError(tr("源图片发生变化，请重新预览：{path}", path=path))
                _cancel(cancelled)
                for number in range(10000):
                    suffix = "" if number == 0 else f" ({number})"
                    stem = filename[:-7]
                    while len((stem + suffix + ".d3dxcp").encode("utf-16-le")) // 2 > 255:
                        stem = stem[:-1]
                    target = directory / (stem + suffix + ".d3dxcp")
                    try:
                        rename_exclusive(temporary, target)
                        break
                    except FileExistsError:
                        continue
                else:
                    raise CategoryPresetError(tr("同名文件过多，请使用其他名称"))
            try:
                _notify(progress, "已提交", 100)
            except Exception:
                logger.exception("分类预设导出发布后的进度通知失败")
            try:
                self.discard(plan)
            except Exception:
                logger.exception("分类预设已导出，暂存清理失败：%s", plan.staging)
            return ExportResult(plan.target.game_id, target, len(plan.rows), len(written.images))
        finally:
            if owned and matches(temporary, owned):
                temporary.unlink()

    def discard(self, plan):
        """无待恢复导入日志时清理计划暂存内容，无法安全清理的路径保留并报告。"""
        if plan is None:
            return
        with self._connection() as conn:
            pending = conn.execute(
                "SELECT 1 FROM category_import_journal WHERE staging_path=?", (str(plan.staging),)
            ).fetchone()
        if pending:
            return
        errors = self._cleanup_owned(StagingOwnership(plan.staging, plan.stage_entries))
        if errors:
            raise CategoryPresetError(tr("暂存清理未完成，已保留以下路径：") + "、".join(errors))
