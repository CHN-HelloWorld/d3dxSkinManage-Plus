"""根据持久变量归属采集 Mod 运行状态。

只接受已启用 Mod 的有效数值，分别报告无法归属、声明冲突及无效内容。
采集结果保留部署版本和采集代次，供后续操作验证状态来源。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.core.migoto_user_ini import UserIniSnapshot, duplicate_persisted_keys, values_equal
from src.core.mod_runtime_prefs import RuntimeFileInfo, RuntimePrefsStore
from src.core.persist_vars import AttributionIndex
from src.utils.i18n import t as tr


@dataclass(frozen=True)
class CollectReport:
    """汇总已归属变量、无归属键、冲突键、无效数值及偏好是否变化。"""

    attributed: dict[str, dict[str, str]] = field(default_factory=dict)
    unattributed: tuple[str, ...] = ()
    conflicted: tuple[str, ...] = ()
    changed: bool = False
    invalid: tuple[str, ...] = ()

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """将无法采集的变量类别及数量转换为可展示的诊断说明。"""
        out = []
        if self.unattributed:
            out.append(tr("{count} 个键无法归属到已启用 Mod，已跳过", count=len(self.unattributed)))
        if self.conflicted:
            out.append(tr("{count} 个键被重复声明或多个 Mod 同时声明，已跳过", count=len(self.conflicted)))
        if self.invalid:
            out.append(tr("{count} 个变量不是加载器支持的有限数值，已跳过", count=len(self.invalid)))
        return tuple(out)


def collect(
    snapshot: UserIniSnapshot,
    index: AttributionIndex,
    store: RuntimePrefsStore,
    *,
    enabled_mod_ids: Iterable[str] | None = None,
    deployment_generation: str = "",
    capture_generation: int = 0,
) -> CollectReport:
    """将运行时变量快照按唯一声明归属合并进已启用 Mod 的工作状态。

    跳过过期采集、重复键、归属冲突及非有限数值，并记录声明指纹、部署代次和来源证据；本函数不写磁盘。"""
    from src.core.persist_vars import declaration_fingerprint
    from src.models.mod_variant import validate_numeric_value

    latest = max((state.capture_generation for state in store.mods.values()), default=0)
    if capture_generation and capture_generation < latest:
        return CollectReport()
    sequence = capture_generation or latest + 1
    enabled = set(index.keys_by_deployment if enabled_mod_ids is None else enabled_mod_ids)
    attributed: dict[str, dict[str, str]] = {}
    unattributed, conflicted, invalid = set(), set(), set()
    duplicates = {index.canonical_key(k) for k in duplicate_persisted_keys(snapshot.text)}
    conflicted.update(duplicates)
    for raw_key, value in snapshot.values.items():
        key = index.canonical_key(raw_key)
        if key in duplicates:
            continue
        owner = index.resolve(key)
        if owner is None or owner not in enabled:
            (conflicted if key in index.conflicts else unattributed).add(key)
            continue
        try:
            value = validate_numeric_value(value)
        except ValueError:
            invalid.add(key)
            continue
        previous = attributed.setdefault(owner, {}).get(key)
        if previous is not None and not values_equal(previous, value):
            attributed[owner].pop(key, None)
            conflicted.add(key)
            continue
        if key not in conflicted:
            attributed[owner][key] = value

    before = store.to_dict()
    now = datetime.now(timezone.utc).isoformat()
    for mod_id in enabled:
        values = attributed.get(mod_id, {})
        if not values and mod_id not in store.mods:
            continue
        state = store.state_for(mod_id)
        state.values.update(values)
        try:
            fingerprint = declaration_fingerprint(index, mod_id)
        except ValueError:
            fingerprint = ""
        state.declaration_fingerprint = fingerprint
        state.deployment_generation = deployment_generation
        state.capture_generation = sequence
        state.confirmed_keys = tuple(sorted(values)) if fingerprint else ()
        state.captured_at = now
        for key in values:
            state.sources[key] = {
                "source": "game",
                "captured_at": now,
                "declaration_fingerprint": fingerprint,
                "deployment_generation": deployment_generation,
                "capture_generation": sequence,
            }
    store.runtime_file = RuntimeFileInfo(path=str(snapshot.path), last_seen_hash=snapshot.sha256)
    return CollectReport(
        attributed={mid: values for mid, values in attributed.items() if values},
        unattributed=tuple(sorted(unattributed)),
        conflicted=tuple(sorted(conflicted)),
        invalid=tuple(sorted(invalid)),
        changed=before != store.to_dict(),
    )
