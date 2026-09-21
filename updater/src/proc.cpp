#include "proc.h"
#include "logger.h"
#include "manifest.h"
#include "paths.h"
#include "util.h"

#include <shellapi.h>

namespace updater {
namespace {
// 独占当前作用域打开的进程句柄，不拥有或终止目标进程。
struct ProcessHandle {
    HANDLE value;
    // OpenProcess 失败时句柄为空；有效句柄在离开作用域时关闭。
    ~ProcessHandle() {
        if (value)
            CloseHandle(value);
    }
};
}

bool WaitForProcessExit(DWORD pid, DWORD timeoutMs) {
    if (!pid || pid == GetCurrentProcessId()) {
        LogError(L"主程序 PID 无效");
        return false;
    }
    LogInfo(L"等待主程序 PID=" + std::to_wstring(pid) + L" 退出…");
    ProcessHandle process{OpenProcess(SYNCHRONIZE, FALSE, pid)};
    if (!process.value) {
        const DWORD error = GetLastError();
        if (error == ERROR_INVALID_PARAMETER)
            return true;
        LogError(L"无法确认主程序已退出: " + Win32Error(error));
        return false;
    }
    const DWORD result = WaitForSingleObject(process.value, timeoutMs);
    if (result == WAIT_OBJECT_0) {
        LogInfo(L"主程序已退出");
        return true;
    }
    if (result == WAIT_TIMEOUT)
        LogError(L"等待主程序退出超时，已停止更新");
    else
        LogError(L"等待主程序退出失败: " + Win32Error(GetLastError()));
    return false;
}

bool LaunchNewVersion(const std::filesystem::path& installDir) {
    try {
        const auto directory = std::filesystem::absolute(installDir).lexically_normal();
        const auto executable = SafeChild(directory, kMainExeName);
        if (!std::filesystem::is_regular_file(executable)) {
            LogError(L"主程序不存在: " + executable.wstring());
            return false;
        }
        // 主程序需要提权；未提权的更新器直接调用 CreateProcess 会返回错误 740。

        SHELLEXECUTEINFOW launch{};
        launch.cbSize = sizeof(launch);
        launch.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_NO_CONSOLE | SEE_MASK_FLAG_NO_UI;
        launch.lpVerb = L"open";
        launch.lpFile = executable.c_str();
        launch.lpDirectory = directory.c_str();
        launch.nShow = SW_SHOWNORMAL;
        if (!ShellExecuteExW(&launch)) {
            LogError(L"启动新版本失败: " + Win32Error(GetLastError()));
            return false;
        }
        if (launch.hProcess)
            CloseHandle(launch.hProcess);
        LogInfo(L"新版本已启动");
        return true;
    } catch (const std::exception& error) {
        LogError(L"启动新版本失败: " + ExceptionMessage(error));
        return false;
    }
}
}
