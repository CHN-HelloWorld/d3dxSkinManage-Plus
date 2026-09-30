#pragma once

#include <filesystem>
#include <map>
#include <string>
#include <vector>

namespace updater {

// 按类型保存 JSON 节点；字符串保存解码内容，数字和布尔值在 text 中保留字面量。
struct JsonValue {
    // 决定应读取 text、array 还是 object；默认构造的节点为空值。
    enum class Type { Null, Boolean, Number, String, Array, Object };
    Type type = Type::Null;
    std::wstring text;
    std::vector<JsonValue> array;
    std::map<std::wstring, JsonValue> object;
    // 非对象或键不存在时返回 nullptr；返回指针借用当前对象中的节点。
    const JsonValue* Find(const std::wstring& key) const;
};

// 解析完整 UTF-16 文本，允许开头的 BOM；语法、重复键或嵌套深度超限时抛出 invalid_argument。
JsonValue ParseJson(const std::wstring& text);
// 按 UTF-8 读取并解析文件；读取失败、字节数超限或内容无效时抛出异常，不返回部分结果。
JsonValue ReadJsonFile(const std::filesystem::path& path, size_t maxBytes = 8 * 1024 * 1024);

}
