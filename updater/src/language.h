#pragma once

#include <filesystem>
#include <initializer_list>
#include <string>
#include <utility>

namespace updater {

// 单次翻译调用的占位符名和值；同名条目以后出现的值为准。
using FormatValues = std::initializer_list<std::pair<std::wstring, std::wstring>>;
// 从安装目录读取语言设置及回退链并原子发布词表；配置不可用时退回默认语言或源文案。
void InitializeLanguage(const std::filesystem::path& installDir);
// 使用词表快照翻译并单次替换占位符；缺少译文或格式化失败时逐级退回源文案。
// 可与词表初始化并发调用，替换值不会再次被解析为格式模板。
std::wstring Tr(const std::wstring& source, FormatValues values = {});
// 为提取工具标记延迟翻译文案；直接返回借用的源字符串，不执行翻译。
constexpr const wchar_t* N_(const wchar_t* source) {
    return source;
}

}
