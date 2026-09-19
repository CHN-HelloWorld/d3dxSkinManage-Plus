#include "json.h"
#include "util.h"

#include <array>
#include <fstream>
#include <stdexcept>
#include <utility>

namespace updater {
namespace {
// 借用输入文本并维护字符游标；递归解析时限制深度，所有语法错误统一带出当前位置。
class Parser {
  public:
    // 输入文本须在解析器使用期间保持有效且不被修改。
    explicit Parser(const std::wstring& text) : text_(text) {}
    // 读取一个根值并确认剩余内容仅为空白，拒绝拼接的多个 JSON 值。
    JsonValue Parse() {
        if (!text_.empty() && text_[0] == 0xfeff)
            ++position_;
        auto result = Value(0);
        Space();
        if (position_ != text_.size())
            Fail();
        return result;
    }

  private:
    // 将当前游标作为诊断位置抛出，调用方无法继续使用部分解析结果。
    [[noreturn]] void Fail() const {
        throw std::invalid_argument("Invalid JSON at character " + std::to_string(position_));
    }
    // 只跳过 JSON 允许的四种空白字符。
    void Space() {
        while (position_ < text_.size()) {
            const auto c = text_[position_];
            if (c != L' ' && c != L'\t' && c != L'\r' && c != L'\n')
                break;
            ++position_;
        }
    }
    // 仅在当前字符匹配时推进游标；不自动跳过空白。
    bool Take(wchar_t c) {
        if (position_ < text_.size() && text_[position_] == c) {
            ++position_;
            return true;
        }
        return false;
    }
    // 消费必需的分隔符，缺失时立即报告语法错误。
    void Expect(wchar_t c) {
        if (!Take(c))
            Fail();
    }
    // 数字语法仅接受 ASCII 十进制数字。
    static bool Digit(wchar_t c) {
        return c >= L'0' && c <= L'9';
    }
    // 消费至少一位十进制数字，用于整数、小数和指数部分。
    void Digits() {
        const auto start = position_;
        while (position_ < text_.size() && Digit(text_[position_]))
            ++position_;
        if (position_ == start)
            Fail();
    }
    // 读取 Unicode 转义中的四位十六进制数；截断或非法字符均报错。
    wchar_t Hex4() {
        unsigned value = 0;
        for (int i = 0; i < 4; ++i) {
            if (position_ == text_.size())
                Fail();
            const auto c = text_[position_++];
            unsigned digit = 0;
            if (c >= L'0' && c <= L'9')
                digit = c - L'0';
            else if (c >= L'a' && c <= L'f')
                digit = c - L'a' + 10;
            else if (c >= L'A' && c <= L'F')
                digit = c - L'A' + 10;
            else
                Fail();
            value = value * 16 + digit;
        }
        return static_cast<wchar_t>(value);
    }
    // 解码带引号的字符串，校验转义、控制字符及 UTF-16 代理项配对。
    std::wstring String() {
        Expect(L'"');
        std::wstring result;
        while (position_ < text_.size()) {
            auto c = text_[position_++];
            if (c == L'"')
                return result;
            if (c < 0x20)
                Fail();
            const bool escaped = c == L'\\';
            if (escaped) {
                if (position_ == text_.size())
                    Fail();
                switch (text_[position_++]) {
                case L'"':
                    c = L'"';
                    break;
                case L'\\':
                    c = L'\\';
                    break;
                case L'/':
                    c = L'/';
                    break;
                case L'b':
                    c = L'\b';
                    break;
                case L'f':
                    c = L'\f';
                    break;
                case L'n':
                    c = L'\n';
                    break;
                case L'r':
                    c = L'\r';
                    break;
                case L't':
                    c = L'\t';
                    break;
                case L'u':
                    c = Hex4();
                    break;
                default:
                    Fail();
                }
            }
            if (c >= 0xd800 && c <= 0xdbff) {
                result.push_back(c);
                if (escaped) {
                    Expect(L'\\');
                    Expect(L'u');
                    c = Hex4();
                } else {
                    if (position_ == text_.size())
                        Fail();
                    c = text_[position_++];
                }
                if (c < 0xdc00 || c > 0xdfff)
                    Fail();
            } else if (c >= 0xdc00 && c <= 0xdfff)
                Fail();
            result.push_back(c);
        }
        Fail();
    }
    // 从当前游标读取节点；对象拒绝重复键，子节点深度超过 64 时终止解析。
    JsonValue Value(unsigned depth) {
        if (depth > 64)
            Fail();
        Space();
        if (position_ == text_.size())
            Fail();
        JsonValue result;
        if (Take(L'{')) {
            result.type = JsonValue::Type::Object;
            Space();
            if (Take(L'}'))
                return result;
            for (;;) {
                Space();
                auto key = String();
                Space();
                Expect(L':');
                auto value = Value(depth + 1);
                if (!result.object.emplace(std::move(key), std::move(value)).second)
                    Fail();
                Space();
                if (Take(L'}'))
                    return result;
                Expect(L',');
            }
        }
        if (Take(L'[')) {
            result.type = JsonValue::Type::Array;
            Space();
            if (Take(L']'))
                return result;
            for (;;) {
                result.array.push_back(Value(depth + 1));
                Space();
                if (Take(L']'))
                    return result;
                Expect(L',');
            }
        }
        if (text_[position_] == L'"') {
            result.type = JsonValue::Type::String;
            result.text = String();
            return result;
        }
        for (const auto* literal : {L"true", L"false", L"null"}) {
            const std::wstring word = literal;
            if (text_.compare(position_, word.size(), word) == 0) {
                position_ += word.size();
                result.type = word == L"null" ? JsonValue::Type::Null : JsonValue::Type::Boolean;
                result.text = word;
                return result;
            }
        }
        const auto start = position_;
        Take(L'-');
        if (!Take(L'0'))
            Digits();
        if (Take(L'.'))
            Digits();
        if (Take(L'e') || Take(L'E')) {
            if (!Take(L'+'))
                Take(L'-');
            Digits();
        }
        result.type = JsonValue::Type::Number;
        result.text = text_.substr(start, position_ - start);
        return result;
    }
    const std::wstring& text_;
    size_t position_ = 0;
};
}

const JsonValue* JsonValue::Find(const std::wstring& key) const {
    if (type != Type::Object)
        return nullptr;
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &it->second;
}

JsonValue ParseJson(const std::wstring& text) {
    return Parser(text).Parse();
}

JsonValue ReadJsonFile(const std::filesystem::path& path, size_t maxBytes) {
    std::ifstream file(path, std::ios::binary);
    if (!file)
        throw std::runtime_error("Cannot read JSON file");
    std::string contents;
    std::array<char, 65536> buffer{};
    while (file) {
        file.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const auto count = static_cast<size_t>(file.gcount());
        if (count > maxBytes - contents.size())
            throw std::length_error("JSON file is too large");
        contents.append(buffer.data(), count);
    }
    if (file.bad())
        throw std::runtime_error("Cannot finish reading JSON file");
    return ParseJson(Utf8ToWide(contents));
}
}
