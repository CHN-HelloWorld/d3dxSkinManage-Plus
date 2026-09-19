#pragma once

#include <filesystem>
#include <string>

namespace updater {

// 生成忽略大小写的 Windows 路径键，统一分隔符并去掉尾部分隔符；不校验路径安全性。
// 使用固定区域设置转换大小写，系统转换失败时抛出 system_error。
std::wstring PathKey(const std::wstring& relative);
// 检查相对路径的语法，拒绝越级、空分量、常见保留设备名、备用数据流及非法字符；允许单个尾斜杠。
bool IsSafeRelative(const std::wstring& relative);
// 按绝对路径的词法形式判断同一路径或后代关系，忽略大小写；不解析链接或检查磁盘存在性。
bool IsWithin(const std::filesystem::path& path, const std::filesystem::path& root);
// 检查 Windows 重解析点属性；路径不存在返回 false，其他属性读取错误抛出 system_error。
bool IsReparsePoint(const std::filesystem::path& path);
// 验证源目录存在、目标类型及祖先无重解析点；目标不能位于源中，嵌套源仅允许在 Updater/Cache。
// 校验失败抛出异常；允许目标尚不存在，不创建目录。
void ValidateUpdateDirectories(const std::filesystem::path& source, const std::filesystem::path& target);
// 将安全的相对路径拼接为绝对路径，并检查包括根在内的现有分量；拒绝越界语法和重解析点。
// 失败抛出异常；不创建文件，也不锁定检查后的目录结构，调用方仍须在操作前重新校验。
std::filesystem::path SafeChild(const std::filesystem::path& root, const std::wstring& relative);

}
