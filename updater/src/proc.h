#pragma once

#include <windows.h>
#include <filesystem>

namespace updater {

// 阻塞等待进程退出；PID 已不存在视为成功，无效 PID、自身 PID、超时或无法确认退出返回 false。
// 只等待和关闭本地句柄，不终止目标进程。
bool WaitForProcessExit(DWORD pid, DWORD timeoutMs);
// 校验主程序路径后通过 ShellExecuteExW 启动，支持权限清单；失败记录日志并返回 false。
// 成功仅表示系统接受了启动请求，不等待程序退出或执行健康检查。
bool LaunchNewVersion(const std::filesystem::path& installDir);

}
