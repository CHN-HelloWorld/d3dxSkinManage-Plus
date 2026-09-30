#pragma once

#include <windows.h>
#include <exception>
#include <string>
#include <vector>

namespace updater {

// 严格将 UTF-8 转为 UTF-16，保留内嵌空字符；无效编码或长度超过 Win32 限制时抛出异常。
std::wstring Utf8ToWide(const std::string& text);
// 严格将 UTF-16 转为 UTF-8，保留内嵌空字符；无效代理项或长度超过 Win32 限制时抛出异常。
std::string WideToUtf8(const std::wstring& text);
// 解析命令行保留列表；要求完整的 JSON 字符串数组，结构或元素类型错误时抛出 invalid_argument。
// Unicode 转义按 UTF-16 码元保留，此解析器不验证代理项配对。
std::vector<std::wstring> ParseJsonStringArray(const std::wstring& json);
// 仅将反斜杠替换为斜杠；不折叠路径分量、不转换大小写，也不校验路径安全性。
std::wstring NormalizeRel(const std::wstring& path);
// 按 wcsftime 模板格式化当前本地时间；时间转换失败或结果超出固定缓冲区时返回空串。
std::wstring FormatTime(const wchar_t* format);
// 返回含三位毫秒的本地时间，格式为 YYYY-MM-DD HH:MM:SS.mmm。
std::wstring FormatTimeMs();
// 返回去掉尾部空格和换行的系统错误说明并附数值代码；系统无说明时使用通用文案。
std::wstring Win32Error(DWORD error);
// 优先按 UTF-8 解码异常说明，编码无效时改用系统代码页；无法获取转换长度时返回中文提示。
std::wstring ExceptionMessage(const std::exception& error);

}
