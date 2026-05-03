"""管理更新工作进程，并在操作系统层面暂停文件读写。

工作进程收到启动指令前等待，以便先纳入关闭时终止的 Windows 作业。
确认进程退出后才向应用发布终态。
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from src.core.mod_update_paths import load_update_result, store_update_plan
from src.utils.i18n import t as tr
from src.utils.paths import is_frozen


class _WindowsJob:
    """不可继承的 Windows 作业拥有工作进程及其全部后代。"""

    def __init__(self):
        """初始化 Windows 作业与进程控制接口，启用关闭作业时终止全部成员的限制。"""
        from ctypes import wintypes

        self._handle = None
        self._root_handle = None
        self._pid: int | None = None
        self._terminating = False
        # 恢复前保留进程句柄，避免已退出的进程号被其他进程复用。
        self._suspended: dict[int, int] = {}
        self._finishing: dict[int, int] = {}
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        kernel = self._kernel
        kernel.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        kernel.IsProcessInJob.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        for name in ("NtSuspendProcess", "NtResumeProcess"):
            function = getattr(self._ntdll, name)
            function.argtypes = (wintypes.HANDLE,)
            function.restype = wintypes.LONG

        class BasicLimits(ctypes.Structure):
            """映射 Windows 作业基本资源及行为限制结构。"""

            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            """映射作业读写次数和传输字节数统计字段。"""

            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            """组合基本限制、读写统计与内存上限，供设置扩展作业信息。"""

            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, pid: int) -> None:
        # 分配作业需要设置配额和终止进程的访问权限。
        """打开工作进程控制句柄并加入当前作业，保留根句柄以确认真实退出。"""
        handle = self._kernel.OpenProcess(0x0100 | 0x0001 | 0x0800 | 0x00100000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._root_handle = handle
        if not self._kernel.AssignProcessToJobObject(self._handle, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self._pid = pid

    def release_startup(self) -> None:
        """恢复以挂起状态创建的根进程，原生恢复失败时抛出错误。"""
        status = self._ntdll.NtResumeProcess(self._root_handle)
        if status < 0:
            raise OSError(tr("无法启动更新进程（NTSTATUS 0x{value:08X}）", value=status & 4294967295))

    def _process_ids(self) -> list[int]:
        """查询作业当前成员进程号，缓冲不足时扩容后重试。"""
        from ctypes import wintypes

        if self._handle is None:
            return []
        capacity = 16
        while True:

            class ProcessIds(ctypes.Structure):
                """映射按当前容量分配的作业进程号列表。"""

                _fields_ = [
                    ("assigned", wintypes.DWORD),
                    ("count", wintypes.DWORD),
                    ("pids", ctypes.c_size_t * capacity),
                ]

            result = ProcessIds()
            if self._kernel.QueryInformationJobObject(
                self._handle,
                3,
                ctypes.byref(result),
                ctypes.sizeof(result),
                None,
            ):
                return [int(result.pids[index]) for index in range(result.count)]
            error = ctypes.get_last_error()
            if error != 234:  # ERROR_MORE_DATA
                raise ctypes.WinError(error)
            capacity = max(capacity * 2, result.assigned)

    def _suspend_pid(self, pid: int) -> None:
        """确认进程仍属于作业后暂停，并保留句柄以避免进程号复用；已退出目标跳过。"""
        from ctypes import wintypes

        if pid in self._suspended or pid in self._finishing:
            return
        # SUSPEND_RESUME | QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        handle = self._kernel.OpenProcess(0x0800 | 0x1000 | 0x00100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87 or pid not in self._process_ids():  # 进程已经退出。
                return
            raise ctypes.WinError(error)
        try:
            belongs = wintypes.BOOL()
            if not self._kernel.IsProcessInJob(handle, self._handle, ctypes.byref(belongs)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not belongs.value or self._kernel.WaitForSingleObject(handle, 0) == 0:
                return
            status = self._ntdll.NtSuspendProcess(handle)
            if status < 0:
                if status & 0xFFFFFFFF == 0xC000010A:  # STATUS_PROCESS_IS_TERMINATING
                    self._finishing[pid] = handle
                    handle = None
                    return
                if self._kernel.WaitForSingleObject(handle, 0) == 0:
                    return
                raise OSError(
                    tr("无法暂停更新进程 {pid}（NTSTATUS 0x{value:08X}）", pid=pid, value=status & 4294967295)
                )
            self._suspended[pid] = handle
            handle = None
        finally:
            if handle is not None:
                self._kernel.CloseHandle(handle)

    def pause(self) -> None:
        """先暂停根进程，再反复暂停新发现后代；失败时尝试恢复已经暂停的成员。"""
        try:
            # 先暂停父进程，再持续发现并暂停后代，直到所有已观察进程均无法继续派生。
            if self._pid is not None:
                self._suspend_pid(self._pid)
            seen = set(self._suspended) | set(self._finishing)
            while True:
                pending = set(self._process_ids()) - seen
                if not pending:
                    return
                for pid in pending:
                    seen.add(pid)
                    self._suspend_pid(pid)
        except OSError:
            with contextlib.suppress(OSError):
                self.resume()
            raise

    def resume(self) -> None:
        """按暂停逆序恢复进程并关闭保留句柄，汇总仍未退出进程的恢复失败。"""
        failure = None
        suspended, self._suspended = self._suspended, {}
        # 先恢复现有后代，最后恢复根进程。
        for pid, handle in reversed(list(suspended.items())):
            status = self._ntdll.NtResumeProcess(handle)
            if status < 0 and self._kernel.WaitForSingleObject(handle, 0) != 0:
                failure = OSError(
                    tr("无法继续更新进程 {pid}（NTSTATUS 0x{value:08X}）", pid=pid, value=status & 4294967295)
                )
            self._kernel.CloseHandle(handle)
        if failure is not None:
            raise failure

    def terminate(self) -> None:
        """先尽力暂停进程树阻止继续派生，再终止整个作业。"""
        if not self._terminating:
            self._terminating = True
            # 终止前保存可等待的句柄，并先阻止继续派生进程；关闭应用时同样处理。
            with contextlib.suppress(OSError):
                self.pause()
        if self._handle is not None and not self._kernel.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def root_exited(self) -> bool:
        """通过根进程句柄判断退出是否已完成，未持有根句柄时视为已退出。"""
        return self._root_handle is None or self._kernel.WaitForSingleObject(self._root_handle, 0) == 0

    def is_running(self) -> bool:
        # 退出码和空作业列表可能早于进程句柄完成信号，须等待句柄后才能清理文件。
        """同时检查作业成员及保留句柄，直到全部退出完成才允许清理文件。"""
        return (
            bool(self._process_ids())
            or not self.root_exited()
            or any(self._kernel.WaitForSingleObject(handle, 0) != 0 for handle in self._suspended.values())
            or any(self._kernel.WaitForSingleObject(handle, 0) != 0 for handle in self._finishing.values())
        )

    def close(self) -> None:
        """关闭作业、根进程及暂停和退出中成员的句柄，并清空所有登记。"""
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
        if self._root_handle is not None:
            self._kernel.CloseHandle(self._root_handle)
            self._root_handle = None
        for handle in self._suspended.values():
            self._kernel.CloseHandle(handle)
        self._suspended.clear()
        for handle in self._finishing.values():
            self._kernel.CloseHandle(handle)
        self._finishing.clear()

    def __del__(self):
        """对象回收时尽力释放原生句柄，忽略析构期间异常。"""
        with contextlib.suppress(Exception):
            self.close()


class _PosixProcessGroup:
    """通过独立 POSIX 进程组控制更新工作进程及其后代。"""

    def __init__(self):
        """初始化尚未绑定的工作进程组编号。"""
        self._pid: int | None = None
