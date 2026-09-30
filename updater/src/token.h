#pragma once

#include <cstdint>
#include <string>

namespace updater {

// 按主程序共享的 HMAC-SHA256 协议校验“十六进制时间戳.小写十六进制摘要”。
// now 为 Unix 秒数，时间偏差最多 120 秒；格式、时间、摘要或加密 API 失败均返回 false。
// 令牌用于约束启动入口，不绑定 PID、路径或保留规则，调用方仍须校验这些参数。
bool VerifyLaunchToken(const std::wstring& token, std::int64_t now);
// 使用当前系统 Unix 时间执行同样的校验。
bool VerifyLaunchToken(const std::wstring& token);

}
