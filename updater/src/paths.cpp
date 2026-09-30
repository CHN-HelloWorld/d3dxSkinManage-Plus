#include "paths.h"
#include "util.h"

#include <stdexcept>
#include <system_error>
#include <utility>

namespace updater {
namespace fs = std::filesystem;

std::wstring PathKey(const std::wstring& relative) {
    auto result = NormalizeRel(relative);
    while (!result.empty() && result.back() == L'/')
        result.pop_back();
    if (!result.empty()) {
        const int length = static_cast<int>(result.size());
        std::wstring lower(result.size(), L'\0');
        if (!LCMapStringEx(LOCALE_NAME_INVARIANT, LCMAP_LOWERCASE, result.data(), length, lower.data(), length, nullptr,
                           nullptr, 0)) {
            throw std::system_error(static_cast<int>(GetLastError()), std::system_category(), "LCMapStringEx");
        }
        result = std::move(lower);
    }
    return result;
}

bool IsSafeRelative(const std::wstring& relative) {
    auto normalized = NormalizeRel(relative);
    if (normalized.empty() || normalized.front() == L'/')
        return false;
    if (normalized.back() == L'/')
        normalized.pop_back();
    size_t start = 0;
    for (size_t i = 0; i <= normalized.size(); ++i) {
        if (i != normalized.size() && normalized[i] != L'/') {
            const auto c = normalized[i];
            if (c < 32 || c == L':' || c == L'"' || c == L'<' || c == L'>' || c == L'|' || c == L'*' || c == L'?')
                return false;
            continue;
        }
        const auto part = normalized.substr(start, i - start);
        if (part.empty() || part == L"." || part == L".." || part.back() == L'.' || part.back() == L' ')
            return false;
        const auto basename = PathKey(part.substr(0, part.find(L'.')));
        if (basename == L"con" || basename == L"prn" || basename == L"aux" || basename == L"nul" ||
            (basename.size() == 4 && (basename.compare(0, 3, L"com") == 0 || basename.compare(0, 3, L"lpt") == 0) &&
             basename[3] >= L'1' && basename[3] <= L'9'))
            return false;
        start = i + 1;
    }
    return !normalized.empty();
}

bool IsWithin(const fs::path& path, const fs::path& root) {
    const auto child = PathKey(fs::absolute(path).lexically_normal().wstring());
    const auto parent = PathKey(fs::absolute(root).lexically_normal().wstring());
    return child == parent || (child.size() > parent.size() && child.compare(0, parent.size(), parent) == 0 &&
                               child[parent.size()] == L'/');
}

bool IsReparsePoint(const fs::path& path) {
    const DWORD attributes = GetFileAttributesW(path.c_str());
    if (attributes == INVALID_FILE_ATTRIBUTES) {
        const DWORD error = GetLastError();
        if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
            return false;
        throw std::system_error(static_cast<int>(error), std::system_category(), "GetFileAttributesW");
    }
    return (attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0;
}

namespace {
// 从根开始逐级检查绝对路径，拒绝任意现存的重解析点；不存在的分量由属性查询允许通过。
void CheckComponents(const fs::path& path) {
    fs::path current;
    for (const auto& part : fs::absolute(path).lexically_normal()) {
        current /= part;
        if (!current.has_root_directory())
            continue;
        if (IsReparsePoint(current))
            throw std::invalid_argument("Update paths must not cross a junction or symbolic link");
    }
}
}

void ValidateUpdateDirectories(const fs::path& source, const fs::path& target) {
    if (source.empty() || target.empty())
        throw std::invalid_argument("Update directory is empty");
    CheckComponents(source);
    CheckComponents(target);
    if (!fs::is_directory(source))
        throw std::invalid_argument("Extraction directory does not exist");
    if (fs::exists(target) && !fs::is_directory(target))
        throw std::invalid_argument("Installation path is not a directory");
    if (IsWithin(target, source))
        throw std::invalid_argument("Extraction directory must not contain the installation directory");
    // 解压目录位于受保护的缓存中，不属于已安装文件。
    if (IsWithin(source, target) && !IsWithin(source, target / L"Updater" / L"Cache")) {
        throw std::invalid_argument("Nested extraction directory must be in Updater/Cache");
    }
}

fs::path SafeChild(const fs::path& root, const std::wstring& relative) {
    if (!IsSafeRelative(relative))
        throw std::invalid_argument("Unsafe relative update path");
    const auto result = fs::absolute(root).lexically_normal() / fs::path(NormalizeRel(relative));
    CheckComponents(result);
    return result;
}

}
