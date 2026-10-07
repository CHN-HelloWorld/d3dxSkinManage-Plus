"""插件资源句柄及按所有权逆序释放的资源集合，不依赖 Qt。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol, TypeVar, overload


class DisposableLike(Protocol):
    """可交由插件上下文或局部作用域管理的资源协议。"""

    def dispose(self) -> None:
        """释放资源；重复调用应保持无副作用。"""
        ...


class Disposable:
    """将释放回调封装为幂等句柄，并支持 with 作用域。"""

    def __init__(self, callback: Callable[[], None]) -> None:
        """保存资源释放回调，拒绝不可调用的值。"""
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        self._callback: Callable[[], None] | None = callback

    @property
    def disposed(self) -> bool:
        """返回句柄是否已释放。"""
        return self._callback is None

    def dispose(self) -> None:
        """先解除回调引用再释放资源，保证异常和重入下也只释放一次。"""
        callback, self._callback = self._callback, None
        if callback is not None:
            callback()

    def __enter__(self) -> Disposable:
        """进入资源作用域并返回本句柄。"""
        return self

    def __exit__(self, *_exc) -> None:
        """退出作用域时释放资源。"""
        self.dispose()


_Resource = TypeVar("_Resource", bound=DisposableLike)


class DisposableStore:
    """拥有一组资源；关闭时逆序释放，单项异常不阻止其余清理。"""

    def __init__(self) -> None:
        """创建空的资源集合。"""
        self._items: list[DisposableLike] = []
        self._disposed = False

    @property
    def disposed(self) -> bool:
        """返回集合是否已关闭。"""
        return self._disposed

    @overload
    def add(self, resource: _Resource) -> _Resource:
        """保留并返回实现 dispose 协议的原始资源。"""
        ...

    @overload
    def add(self, resource: Callable[[], None]) -> Disposable:
        """将清理函数包装为可单独释放的句柄。"""
        ...

    def add(self, resource):
        """登记资源；集合已关闭时立即清理新增资源，避免停用过程产生泄漏。"""
        if resource is self:
            raise ValueError("资源集合不能拥有自身")
        if not callable(getattr(resource, "dispose", None)):
            resource = Disposable(resource)
        if self._disposed:
            self._release(resource)
        else:
            self._items.append(resource)
        return resource

    @staticmethod
    def _release(resource: DisposableLike) -> None:
        """隔离单个资源的释放异常，保留完整日志。"""
        try:
            resource.dispose()
        except Exception:
            logging.getLogger(__name__).exception("插件资源释放失败")

    def dispose(self) -> None:
        """先关闭集合再逆序释放全部资源，允许回调重入。"""
        if self._disposed:
            return
        self._disposed = True
        items, self._items = self._items, []
        for resource in reversed(items):
            self._release(resource)

    def __enter__(self) -> DisposableStore:
        """进入组合资源作用域。"""
        return self

    def __exit__(self, *_exc) -> None:
        """退出作用域时释放全部资源。"""
        self.dispose()
