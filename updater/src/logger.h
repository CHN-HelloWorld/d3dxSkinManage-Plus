#pragma once

#include <filesystem>
#include <string>

namespace updater {

// 在安装目录的 logs/updater 下追加 UTF-8 日志；初始化失败时停用文件日志，不阻断更新。
// 日志初始化、写入和关闭通过同一互斥锁串行执行，可由界面线程和工作线程调用。
void InitLog(const std::filesystem::path& installDir);
// 写入带本地时间的普通记录并刷新；未打开日志时忽略。
void LogInfo(const std::wstring& message);
// 写入警告记录并刷新，写入失败不影响文件恢复流程。
void LogWarning(const std::wstring& message);
// 写入错误记录并刷新，写入失败不影响文件恢复流程。
void LogError(const std::wstring& message);
// 记录结束时间并关闭共享日志；重复关闭无操作。
void CloseLog();

}
