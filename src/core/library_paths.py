"""按索引中的父节点和名称逐级解析资源位置，不建立全库路径缓存。"""

from __future__ import annotations

import os
from pathlib import Path

from src.core.library_schema import directory_key, register_connection, valid_relative
from src.utils.i18n import t as tr


class LibraryPaths:
    """通过游戏根和数据库父链实时解析分类、Mod 容器及图片的路径。"""

    def __init__(self, connection, roots, preview_roots=None):
        """注册数据库路径校验函数，并保存绝对资料库根与预览缓存根映射。"""
        self.connection = connection
        register_connection(connection)
        self.roots = {gid: Path(os.path.abspath(root)) for gid, root in roots.items()}
        self.preview_roots = dict(preview_roots or {})

    def _node(self, node_id):
        """读取节点所属游戏、父节点和目录名，节点不存在时抛出 KeyError。"""
        row = self.connection.execute(
            "SELECT id,game_id,parent_id,directory_name FROM nodes WHERE id=?", (node_id,)
        ).fetchone()
        if row is None:
            raise KeyError(node_id)
        return tuple(row)

    def _mod(self, mod_id):
        """读取 Mod 所属游戏、分类、容器名和正文路径，不存在时抛出 KeyError。"""
        row = self.connection.execute(
            "SELECT game_id,node_id,folder_name,path FROM mods WHERE mod_id=?", (mod_id,)
        ).fetchone()
        if row is None:
            raise KeyError(mod_id)
        return tuple(row)

    def _chain(self, node_id):
        """沿父节点收集分类链，检测循环引用和跨游戏父链并拒绝无效结构。"""
        chain, visited = [], set()
        while node_id is not None:
            if node_id in visited:
                raise ValueError(tr("节点父链循环: {node_id}", node_id=node_id))
            visited.add(node_id)
            row = self._node(node_id)
            chain.append(row)
            node_id = row[2]
        if chain and any(r[1] != chain[0][1] for r in chain):
            raise ValueError(tr("节点父链跨游戏"))
        return chain

    def node_path(self, node_id: str) -> Path:
        """按已验证的父链拼接分类绝对路径，缺失节点或根配置会抛出异常。"""
        chain = self._chain(node_id)
        root = self.roots[chain[0][1]]
        return root.joinpath(*(r[3] for r in reversed(chain)))

    def container_path(self, mod_id: str) -> Path:
        """使用当前分类路径或游戏根拼接 Mod 容器目录。"""
        game_id, node_id, folder, _ = self._mod(mod_id)
        return (self.node_path(node_id) if node_id else self.roots[game_id]) / folder

    def body_path(self, mod_id: str) -> Path:
        """将 Mod 的正文相对路径拼接到当前容器目录。"""
        game_id, node_id, folder, body = self._mod(mod_id)
        return (self.node_path(node_id) if node_id else self.roots[game_id]) / folder / body

    def locate(self, absolute_path: str) -> tuple[str, str] | None:
        """从最具体的游戏根逐级查询路径归属，返回已登记 Mod 或最近分类身份。"""
        target = os.path.abspath(absolute_path)
        target_key = directory_key(target)
        for gid, root in sorted(self.roots.items(), key=lambda r: len(str(r[1])), reverse=True):
            root_key = directory_key(str(root)).rstrip("\\")
            if not target_key.startswith(root_key + "\\"):
                continue
            parts = target_key[len(root_key) + 1 :].split("\\")
            parent = None
            for part in parts:
                if parent is None:
                    node = self.connection.execute(
                        "SELECT id FROM nodes WHERE parent_id IS NULL AND game_id=? "
                        "AND directory_key(directory_name)=?",
                        (gid, part),
                    ).fetchone()
                else:
                    node = self.connection.execute(
                        "SELECT id FROM nodes WHERE parent_id=? AND directory_key(directory_name)=?", (parent, part)
                    ).fetchone()
                if node:
                    parent = node[0]
                    continue
                if parent is None:
                    mod = self.connection.execute(
                        "SELECT mod_id FROM mods WHERE node_id IS NULL AND game_id=? AND directory_key(folder_name)=?",
                        (gid, part),
                    ).fetchone()
                else:
                    mod = self.connection.execute(
                        "SELECT mod_id FROM mods WHERE node_id=? AND directory_key(folder_name)=?", (parent, part)
                    ).fetchone()
                return ("mod", mod[0]) if mod else (("node", parent) if parent else None)
            return ("node", parent) if parent else None
        return None

    def invalidate_node(self, node_id: str) -> None:
        # 按需解析索引祖先，无需重写后代路径。
        """接受节点失效通知；路径由数据库实时计算，因此无需清理缓存。"""
        pass

    def version(self, mod_id):
        """组合游戏根、分类父链、容器名与正文路径，生成路径结构版本。"""
        game_id, node_id, folder, body = self._mod(mod_id)
        return (str(self.roots[game_id]), tuple(self._chain(node_id)), folder, body)

    def move_node(self, node_id, parent_id, directory_name=None):
        """在调用方事务中提交已确认的节点位置。

        文件移动完成或监控验证新位置标记后调用。数据库约束原子拒绝循环、
        跨游戏变化、无效名称和已占用目标。
        """
        row = self._node(node_id)
        name = row[3] if directory_name is None else directory_name
        owns_transaction = not self.connection.in_transaction
        self.connection.execute("UPDATE nodes SET parent_id=?,directory_name=? WHERE id=?", (parent_id, name, node_id))
        if owns_transaction:
            self.connection.commit()
        self.invalidate_node(node_id)

    def resolve_image(self, owner_id, reference):
        """校验图片相对引用并按容器、节点或预览缓存归属解析绝对路径。"""
        value = reference["path"]
        if not valid_relative(value):
            raise ValueError(tr("图片引用越界: {value!r}", value=value))
        scope = reference["scope"]
        if scope == "container":
            root = self.container_path(owner_id)
        elif scope == "node":
            root = self.node_path(owner_id)
        elif scope == "preview_cache":
            game_id = self._mod(owner_id)[0]
            root = Path(self.preview_roots[game_id]) / owner_id
        else:
            raise ValueError(tr("未知图片归属: {scope}", scope=scope))
        return root / value
