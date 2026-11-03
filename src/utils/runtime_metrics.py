"""有界保留常驻性能样本，维持列表序列化及跨淘汰的增量采样。"""

from __future__ import annotations

from copy import deepcopy


class MetricSamples(list):
    """顺序追加的近期样本列表；分块淘汰旧样本，累计数量不受淘汰影响。"""

    def __init__(self, values=(), *, limit: int = 2048) -> None:
        """设置样本上限并收纳初始值，避免长寿命控制器无限保存诊断历史。"""
        if limit < 1:
            raise ValueError("指标样本容量必须为正数")
        super().__init__()
        self.limit = limit
        self.total_count = 0
        self.extend(values)

    def append(self, value) -> None:
        """追加样本，满额时批量淘汰前半段，摊薄列表移动开销。"""
        if len(self) >= self.limit:
            del self[: max(1, self.limit // 2)]
        super().append(value)
        self.total_count += 1

    def extend(self, values) -> None:
        """逐项应用相同容量规则，支持把自身作为来源。"""
        if values is self:
            values = tuple(values)
        for value in values:
            self.append(value)

    def __iadd__(self, values):
        """保持原位追加与 extend 的容量和计数行为一致。"""
        self.extend(values)
        return self

    def copy(self) -> MetricSamples:
        """浅复制近期样本及累计位置，供外部建立采样基线。"""
        result = MetricSamples(self, limit=self.limit)
        result.total_count = self.total_count
        return result

    def __deepcopy__(self, memo):
        """深复制样本时保留累计位置，避免重建列表再次累计已有记录。"""
        result = MetricSamples(limit=self.limit)
        memo[id(self)] = result
        list.extend(result, (deepcopy(value, memo) for value in self))
        result.total_count = self.total_count
        return result


def _sample_count_since(current, previous) -> int:
    """按累计位置计算新增量，同时兼容旧性能脚本构造的普通列表。"""
    before = 0 if previous is None else getattr(previous, "total_count", len(previous))
    return max(0, getattr(current, "total_count", len(current)) - before)


def metric_samples_since(current, previous) -> list:
    """返回基线之后仍在窗口内的新样本，不把已淘汰数量当作列表下标。"""
    count = _sample_count_since(current, previous)
    return list(current[-count:]) if count else []


def dropped_metric_samples(current, previous) -> int:
    """报告基线之后已被淘汰的样本数，使性能报告明确标识截断。"""
    return max(0, _sample_count_since(current, previous) - len(current))
