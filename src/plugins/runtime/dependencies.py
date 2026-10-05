"""插件依赖可用性、激活拓扑及反向停用顺序。"""

from __future__ import annotations

from src.plugin_sdk.errors import PluginError
from src.plugins.runtime.versions import satisfies
from src.utils.i18n import t as tr


class DependencyError(PluginError):
    """依赖缺失、未授权、不兼容或成环时阻止激活。"""


def dependency_order(plugin_id: str, records: dict) -> list[str]:
    """返回依赖先于消费者的激活顺序，检查整个传递依赖闭包。"""
    order: list[str] = []
    visiting: list[str] = []
    visited = set()

    def visit(current: str) -> None:
        """按清单声明递归检查依赖，并在退出节点时追加拓扑序。"""
        if current in visiting:
            raise DependencyError(tr("插件依赖存在循环：{path}", path=" → ".join([*visiting, current])))
        if current in visited:
            return
        record = records.get(current)
        if record is None:
            raise DependencyError(tr("缺少依赖插件：{plugin_id}", plugin_id=current))
        if not record.enabled or not record.trusted:
            raise DependencyError(tr("依赖插件未启用或未获信任：{plugin_id}", plugin_id=current))
        visiting.append(current)
        for dependency, constraint in record.manifest.dependencies.items():
            target = records.get(dependency)
            if target is not None and not satisfies(target.manifest.version, constraint):
                raise DependencyError(
                    tr(
                        "依赖 {plugin_id} 需要 {required}，已安装 {available}",
                        plugin_id=dependency,
                        required=constraint,
                        available=target.manifest.version,
                    )
                )
            visit(dependency)
        visiting.pop()
        visited.add(current)
        order.append(current)

    visit(plugin_id)
    return order


def dependent_order(plugin_id: str, records: dict) -> list[str]:
    """返回消费者先于依赖的停用顺序，坏清单形成的循环也只访问一次。"""
    visited = set()
    order = []

    def visit(current: str) -> None:
        """递归收集直接和间接消费者。"""
        if current in visited:
            return
        visited.add(current)
        for candidate in sorted(records):
            if current in records[candidate].manifest.dependencies:
                visit(candidate)
        order.append(current)

    visit(plugin_id)
    return order
