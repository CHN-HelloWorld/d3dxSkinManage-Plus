#include "language.h"
#include "json.h"
#include "logger.h"
#include "paths.h"
#include "util.h"

#include <algorithm>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>
#include <vector>

namespace updater {
namespace {
namespace fs = std::filesystem;
using Strings = std::map<std::wstring, std::wstring>;
std::shared_ptr<const Strings> catalog = std::make_shared<const Strings>();

// 语言代码仅接受 ASCII 字母，避免依赖系统区域设置。
bool Alpha(wchar_t c) {
    return (c >= L'a' && c <= L'z') || (c >= L'A' && c <= L'Z');
}
// 仅将 ASCII 大写字母转为小写，其余字符保持原样。
wchar_t Lower(wchar_t c) {
    return c >= L'A' && c <= L'Z' ? static_cast<wchar_t>(c + 32) : c;
}
// 仅将 ASCII 小写字母转为大写，其余字符保持原样。
wchar_t Upper(wchar_t c) {
    return c >= L'a' && c <= L'z' ? static_cast<wchar_t>(c - 32) : c;
}

// 统一语言代码的分隔符及语言、地区、文字子标签大小写；不符合允许语法时返回空串。
std::wstring NormalizeCode(const std::wstring& input) {
    if (input.empty() || input.size() > 64 || !Alpha(input[0]))
        return {};
    std::vector<std::wstring> parts(1);
    for (auto c : input) {
        if (c == L'-' || c == L'_') {
            if (parts.back().empty())
                return {};
            parts.emplace_back();
        } else {
            if (!Alpha(c) && !(c >= L'0' && c <= L'9'))
                return {};
            parts.back().push_back(c);
        }
    }
    if (parts.back().empty())
        return {};
    std::wstring result;
    for (size_t i = 0; i < parts.size(); ++i) {
        auto& part = parts[i];
        if (!i)
            std::transform(part.begin(), part.end(), part.begin(), Lower);
        else if (std::all_of(part.begin(), part.end(), Alpha)) {
            if (part.size() == 2)
                std::transform(part.begin(), part.end(), part.begin(), Upper);
            else if (part.size() == 4) {
                std::transform(part.begin(), part.end(), part.begin(), Lower);
                part[0] = Upper(part[0]);
            }
        }
        if (i)
            result += L'-';
        result += part;
    }
    return result;
}

// 单次扫描模板，支持双花括号转义；values 为空时只校验并可收集字段名。
// 括号不匹配或替换值缺失时返回 false，output 可能含部分结果；替换值不再解析。
bool Format(const std::wstring& text, const Strings* values, std::wstring& output,
            std::set<std::wstring>* fields = nullptr) {
    output.clear();
    for (size_t i = 0; i < text.size();) {
        const auto c = text[i++];
        if (c != L'{' && c != L'}') {
            output += c;
            continue;
        }
        if (i < text.size() && text[i] == c) {
            ++i;
            output += c;
            continue;
        }
        if (c == L'}')
            return false;
        const auto end = text.find(L'}', i);
        if (end == std::wstring::npos)
            return false;
        const auto field = text.substr(i, end - i);
        if (field.empty() || field.find(L'{') != std::wstring::npos)
            return false;
        if (fields)
            fields->insert(field);
        if (values) {
            const auto found = values->find(field);
            if (found == values->end())
                return false;
            output += found->second;
        }
        i = end + 1;
    }
    return true;
}

// 源模板合法时要求译文具有相同字段集合；源文本本身不是合法模板时不限制译文格式。
bool Compatible(const std::wstring& source, const std::wstring& translation) {
    std::set<std::wstring> original, translated;
    std::wstring ignored;
    if (!Format(source, nullptr, ignored, &original))
        return true;
    return Format(translation, nullptr, ignored, &translated) && original == translated;
}

// 已校验的消息表及规范化回退语言代码；不保存其他翻译域的条目。
struct Pack {
    Strings messages;
    std::wstring fallback;
};

// 读取扁平或分区语言包，拒绝重解析点和非法结构；忽略空译文、非字符串及字段不匹配项。
// 文件读取、JSON 解析和结构校验异常由调用方处理。
Pack LoadPack(const fs::path& path) {
    if (IsReparsePoint(path))
        throw std::invalid_argument("Language pack is a reparse point");
    const auto root = ReadJsonFile(path);
    if (root.type != JsonValue::Type::Object)
        throw std::invalid_argument("Language pack is not an object");
    const bool structured =
        root.Find(L"_meta") || root.Find(L"messages") || root.Find(L"qt") || root.Find(L"gamebanana");
    Pack result;
    if (structured) {
        const std::set<std::wstring> sections{L"$schema", L"_meta", L"messages", L"qt", L"gamebanana"};
        for (const auto& entry : root.object) {
            if (!sections.count(entry.first))
                throw std::invalid_argument("Unknown language pack section");
        }
        if (const auto* metadata = root.Find(L"_meta")) {
            if (metadata->type != JsonValue::Type::Object)
                throw std::invalid_argument("Invalid language metadata");
            if (const auto* version = metadata->Find(L"format")) {
                if (version->type != JsonValue::Type::Number || version->text != L"1") {
                    throw std::invalid_argument("Unsupported language pack format");
                }
            }
            for (const auto* name : {L"name", L"fallback"}) {
                if (const auto* value = metadata->Find(name)) {
                    if (value->type != JsonValue::Type::String)
                        throw std::invalid_argument("Invalid metadata field");
                }
            }
            if (const auto* fallback = metadata->Find(L"fallback")) {
                result.fallback = NormalizeCode(fallback->text);
                if (!fallback->text.empty() && result.fallback.empty())
                    throw std::invalid_argument("Invalid fallback code");
            }
        }
        for (const auto* domain : {L"messages", L"qt", L"gamebanana"}) {
            if (const auto* table = root.Find(domain)) {
                if (table->type != JsonValue::Type::Object)
                    throw std::invalid_argument("Invalid translation table");
            }
        }
    }
    const auto* messages = structured ? root.Find(L"messages") : &root;
    if (messages) {
        for (const auto& entry : messages->object) {
            if (entry.second.type == JsonValue::Type::String && !entry.second.text.empty() &&
                Compatible(entry.first, entry.second.text)) {
                result.messages.emplace(entry.first, entry.second.text);
            }
        }
    }
    return result;
}

// 按目录名排序发现直属语言包，使用 SafeChild 拒绝链接路径；不可访问项被跳过。
// 规范化后重名的语言只保留排序靠前的包，枚举出错时使用此前已发现的目录。
std::map<std::wstring, fs::path> Discover(const fs::path& root) {
    std::vector<fs::path> folders;
    std::error_code error;
    fs::directory_iterator it(root, error);
    const fs::directory_iterator end;
    for (; !error && it != end; it.increment(error))
        folders.push_back(it->path());
    std::sort(folders.begin(), folders.end(), [](const auto& a, const auto& b) {
        return PathKey(a.filename().wstring()) < PathKey(b.filename().wstring());
    });
    std::map<std::wstring, fs::path> result;
    for (const auto& folder : folders) {
        const auto code = NormalizeCode(folder.filename().wstring());
        if (code.empty())
            continue;
        try {
            const auto path = SafeChild(root, folder.filename().wstring() + L"/strings.json");
            if (fs::is_directory(folder) && fs::is_regular_file(path))
                result.emplace(code, path);
        } catch (const std::exception&) {
            // 单个语言包不可访问时继续加载其他语言。
        }
    }
    return result;
}
}

void InitializeLanguage(const fs::path& installDir) {
    auto strings = std::make_shared<Strings>();
    try {
        std::wstring requested = L"zh-CN";
        try {
            const auto settings = ReadJsonFile(SafeChild(installDir, L"config/settings.json"));
            if (const auto* general = settings.Find(L"general")) {
                if (const auto* language = general->Find(L"language")) {
                    if (language->type == JsonValue::Type::String)
                        requested = NormalizeCode(language->text);
                }
            }
        } catch (const std::exception&) {
        }
        const auto paths = Discover(SafeChild(installDir, L"config/languages"));
        // 仅加载已发现的语言包；缺失或读取失败返回 false，读取异常同时写入日志。
        const auto read = [&](const std::wstring& code, Pack& pack) {
            const auto found = paths.find(code);
            if (found == paths.end())
                return false;
            try {
                pack = LoadPack(found->second);
                return true;
            } catch (const std::exception& error) {
                LogWarning(L"语言配置不可用: " + found->second.wstring() + L": " + ExceptionMessage(error));
                return false;
            }
        };
        Pack selected;
        if (!read(requested, selected)) {
            requested = L"zh-CN";
            read(requested, selected);
        }
        std::vector<Pack> layers;
        layers.push_back(std::move(selected));
        std::set<std::wstring> seen{requested};
        while (!layers.back().fallback.empty() && seen.size() < 16) {
            const auto fallback = layers.back().fallback;
            if (!seen.insert(fallback).second)
                break;
            Pack next;
            if (!read(fallback, next))
                break;
            layers.push_back(std::move(next));
        }
        for (auto it = layers.rbegin(); it != layers.rend(); ++it) {
            for (const auto& entry : it->messages)
                (*strings)[entry.first] = entry.second;
        }
    } catch (const std::exception& error) {
        LogWarning(L"语言配置初始化失败: " + ExceptionMessage(error));
    }
    std::shared_ptr<const Strings> frozen = std::move(strings);
    std::atomic_store(&catalog, std::move(frozen));
}

std::wstring Tr(const std::wstring& source, FormatValues values) {
    const auto current = std::atomic_load(&catalog);
    const auto found = current->find(source);
    const auto& text = found == current->end() ? source : found->second;
    if (values.size() == 0)
        return text;
    Strings fields;
    for (const auto& value : values)
        fields[value.first] = value.second;
    std::wstring result;
    if (Format(text, &fields, result) || Format(source, &fields, result))
        return result;
    return source;
}
}
