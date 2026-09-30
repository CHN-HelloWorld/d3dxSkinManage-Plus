#include "util.h"

#include <algorithm>
#include <ctime>
#include <cwchar>
#include <iterator>
#include <limits>
#include <stdexcept>

namespace updater {

std::wstring Utf8ToWide(const std::string& text) {
    if (text.empty())
        return {};
    if (text.size() > static_cast<size_t>((std::numeric_limits<int>::max)())) {
        throw std::length_error("UTF-8 string is too long");
    }
    const auto length = static_cast<int>(text.size());
    const int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), length, nullptr, 0);
    if (!count)
        throw std::invalid_argument("Invalid UTF-8 text");
    std::wstring result(static_cast<size_t>(count), L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), length, result.data(), count)) {
        throw std::invalid_argument("Invalid UTF-8 text");
    }
    return result;
}

std::string WideToUtf8(const std::wstring& text) {
    if (text.empty())
        return {};
    if (text.size() > static_cast<size_t>((std::numeric_limits<int>::max)())) {
        throw std::length_error("UTF-16 string is too long");
    }
    const auto length = static_cast<int>(text.size());
    const int count =
        WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text.data(), length, nullptr, 0, nullptr, nullptr);
    if (!count)
        throw std::invalid_argument("Invalid UTF-16 text");
    std::string result(static_cast<size_t>(count), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text.data(), length, result.data(), count, nullptr,
                             nullptr)) {
        throw std::invalid_argument("Invalid UTF-16 text");
    }
    return result;
}

namespace {

// 只处理命令行保留列表所需的字符串数组语法，借用输入并用游标记录错误位置。
class JsonStringArrayParser {
  public:
    // 输入文本须在解析期间保持有效且不被修改。
    explicit JsonStringArrayParser(const std::wstring& text) : text_(text) {}

    // 返回全部解码条目，拒绝尾随非空白内容；失败不交付部分数组。
    std::vector<std::wstring> Parse() {
        SkipWhitespace();
        Expect(L'[');
        SkipWhitespace();
        std::vector<std::wstring> result;
        if (!Take(L']')) {
            for (;;) {
                SkipWhitespace();
                result.push_back(ParseString());
                SkipWhitespace();
                if (Take(L']'))
                    break;
                Expect(L',');
            }
        }
        SkipWhitespace();
        if (position_ != text_.size())
            Fail();
        return result;
    }

  private:
    // 抛出包含当前字符位置的 invalid_argument，统一报告结构和转义错误。
    [[noreturn]] void Fail() const {
        throw std::invalid_argument("Expected a JSON array of strings at character " + std::to_string(position_));
    }

    // 仅跳过 JSON 定义的空格、制表符和换行字符。
    void SkipWhitespace() {
        while (position_ < text_.size()) {
            const auto c = text_[position_];
            if (c != L' ' && c != L'\t' && c != L'\r' && c != L'\n')
                break;
            ++position_;
        }
    }

    // 匹配时消费一个字符，否则保持游标并返回 false。
    bool Take(wchar_t c) {
        if (position_ < text_.size() && text_[position_] == c) {
            ++position_;
            return true;
        }
        return false;
    }

    // 消费必需字符；不匹配时终止解析。
    void Expect(wchar_t c) {
        if (!Take(c))
            Fail();
    }

    // 将四位十六进制转义转为一个 UTF-16 码元；不在此校验代理项配对。
    wchar_t ParseUnicodeEscape() {
        unsigned value = 0;
        for (int i = 0; i < 4; ++i) {
            if (position_ == text_.size())
                Fail();
            const auto c = text_[position_++];
            unsigned digit;
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

    // 解码引号内的 JSON 转义，拒绝原始控制字符、非法转义和未闭合字符串。
    std::wstring ParseString() {
        Expect(L'"');
        std::wstring value;
        while (position_ < text_.size()) {
            auto c = text_[position_++];
            if (c == L'"')
                return value;
            if (c < 0x20)
                Fail();
            if (c == L'\\') {
                if (position_ == text_.size())
                    Fail();
                c = text_[position_++];
                switch (c) {
                case L'"':
                case L'\\':
                case L'/':
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
                    c = ParseUnicodeEscape();
                    break;
                default:
                    Fail();
                }
            }
            value.push_back(c);
        }
        Fail();
    }

    const std::wstring& text_;
    size_t position_ = 0;
};

}

std::vector<std::wstring> ParseJsonStringArray(const std::wstring& json) {
    return JsonStringArrayParser(json).Parse();
}

std::wstring NormalizeRel(const std::wstring& path) {
    auto result = path;
    std::replace(result.begin(), result.end(), L'\\', L'/');
    return result;
}

std::wstring FormatTime(const wchar_t* format) {
    const auto now = std::time(nullptr);
    std::tm local{};
    if (localtime_s(&local, &now) != 0)
        return {};
    wchar_t buffer[128]{};
    const auto length = std::wcsftime(buffer, std::size(buffer), format, &local);
    return std::wstring(buffer, length);
}

std::wstring FormatTimeMs() {
    SYSTEMTIME now{};
    GetLocalTime(&now);
    wchar_t buffer[32]{};
    swprintf_s(buffer, L"%04u-%02u-%02u %02u:%02u:%02u.%03u", static_cast<unsigned>(now.wYear),
               static_cast<unsigned>(now.wMonth), static_cast<unsigned>(now.wDay), static_cast<unsigned>(now.wHour),
               static_cast<unsigned>(now.wMinute), static_cast<unsigned>(now.wSecond),
               static_cast<unsigned>(now.wMilliseconds));
    return buffer;
}

std::wstring Win32Error(DWORD error) {
    wchar_t* message = nullptr;
    const DWORD length =
        FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
                       nullptr, error, 0, reinterpret_cast<wchar_t*>(&message), 0, nullptr);
    std::wstring result = length && message ? std::wstring(message, length) : L"Windows error";
    if (message)
        LocalFree(message);
    while (!result.empty() && (result.back() == L'\r' || result.back() == L'\n' || result.back() == L' ')) {
        result.pop_back();
    }
    return result + L" (" + std::to_wstring(error) + L")";
}

std::wstring ExceptionMessage(const std::exception& error) {
    try {
        return Utf8ToWide(error.what());
    } catch (const std::invalid_argument&) {
        const int count = MultiByteToWideChar(CP_ACP, 0, error.what(), -1, nullptr, 0);
        if (!count)
            return L"无法解码错误信息";
        std::wstring result(static_cast<size_t>(count), L'\0');
        MultiByteToWideChar(CP_ACP, 0, error.what(), -1, result.data(), count);
        result.pop_back();
        return result;
    }
}

}
