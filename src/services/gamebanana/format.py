"""文件大小等格式化工具"""

from __future__ import annotations

_UNITS = ["B", "KB", "MB", "GB", "TB"]


def humanize_size(n: int) -> str:
    """字节数 → 自适应单位字符串。1024 进制；>=KB 保留 1 位小数；<1KB 显示整数 B。"""
    if n < 0:
        n = 0
    i = 0
    val = float(n)
    while val >= 1024 and i < len(_UNITS) - 1:
        val /= 1024
        i += 1
    if i == 0:
        return f"{int(val)} {_UNITS[i]}"
    return f"{val:.1f} {_UNITS[i]}"
