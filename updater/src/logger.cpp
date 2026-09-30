#include "logger.h"
#include "paths.h"
#include "util.h"

#include <fstream>
#include <mutex>

namespace updater {
namespace {
std::ofstream logFile;
std::mutex logMutex;

// 调用方须持有 logMutex；编码或流写入异常在此吸收，避免干扰更新和恢复。
void WriteUnlocked(const wchar_t* level, const std::wstring& message) {
    if (!logFile.is_open())
        return;
    try {
        logFile << WideToUtf8(L"[" + FormatTimeMs() + L"] [" + level + L"] " + message) << '\n';
        logFile.flush();
    } catch (const std::exception&) {
        // 日志写入失败不能阻止文件恢复。
    }
}

// 在锁内写完整日志行，防止不同线程的记录交错。
void WriteLog(const wchar_t* level, const std::wstring& message) {
    const std::lock_guard<std::mutex> lock(logMutex);
    WriteUnlocked(level, message);
}
}

void InitLog(const std::filesystem::path& installDir) {
    const std::lock_guard<std::mutex> lock(logMutex);
    if (logFile.is_open())
        logFile.close();
    logFile.clear();
    try {
        const auto directory = SafeChild(installDir, L"logs/updater");
        std::filesystem::create_directories(directory);
        // 同一秒内重复启动时追加日志，保留已有诊断信息。
        // 主程序启动时清理过期的更新器日志。
        const auto relative = L"logs/updater/" + FormatTime(L"%Y%m%d_%H%M%S") + L".log";
        logFile.open(SafeChild(installDir, relative), std::ios::out | std::ios::app | std::ios::binary);
        WriteUnlocked(L"INFO", L"d3dxSkinManage-Plus updater.exe");
        WriteUnlocked(L"INFO", L"启动时间: " + FormatTime(L"%Y-%m-%d %H:%M:%S"));
    } catch (const std::exception&) {
        if (logFile.is_open())
            logFile.close();
    }
}

void LogInfo(const std::wstring& message) {
    WriteLog(L"INFO", message);
}
void LogWarning(const std::wstring& message) {
    WriteLog(L"WARNING", message);
}
void LogError(const std::wstring& message) {
    WriteLog(L"ERROR", message);
}

void CloseLog() {
    const std::lock_guard<std::mutex> lock(logMutex);
    if (!logFile.is_open())
        return;
    WriteUnlocked(L"INFO", L"结束时间: " + FormatTime(L"%Y-%m-%d %H:%M:%S"));
    logFile.close();
}
}
