#include "manifest.h"
#include "logger.h"
#include "paths.h"
#include "util.h"

#include <fstream>
#include <iterator>
#include <sstream>
#include <stdexcept>

namespace updater {
namespace {
namespace fs = std::filesystem;

// 使用固定 Unicode 空白集合解析清单，避免系统区域设置改变行首尾裁剪结果。
bool IsSpace(wchar_t c) {
    return c == L' ' || (c >= L'\t' && c <= L'\r') || (c >= 0x1c && c <= 0x1f) || c == 0x85 || c == 0xa0 ||
           c == 0x1680 || (c >= 0x2000 && c <= 0x200a) || c == 0x2028 || c == 0x2029 || c == 0x202f || c == 0x205f ||
           c == 0x3000;
}

// 去掉清单行两端的空白，保留路径内部字符。
std::wstring Trim(const std::wstring& text) {
    size_t first = 0;
    size_t last = text.size();
    while (first < last && IsSpace(text[first]))
        ++first;
    while (last > first && IsSpace(text[last - 1]))
        --last;
    return text.substr(first, last - first);
}

// 单份清单解析出的受管与保护路径，尚未合并命令行规则或已安装基线。
struct ParsedManifest {
    std::vector<std::wstring> managed;
    std::vector<std::wstring> preserve;
};

// 读取有大小限制的 UTF-8 清单，接受 BOM 及多种换行，要求存在 managed 节。
// 拒绝重解析点和不安全路径；未知节被忽略，读取与校验失败抛出异常。
ParsedManifest ParseManifest(const fs::path& path) {
    if (IsReparsePoint(path) || fs::file_size(path) > 32 * 1024 * 1024) {
        throw std::invalid_argument("Invalid manifest file");
    }
    std::ifstream file(path, std::ios::binary);
    if (!file)
        throw std::runtime_error("Cannot read manifest");
    file.exceptions(std::ios::badbit);
    const std::string contents{std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
    auto decoded = Utf8ToWide(contents);
    if (!decoded.empty() && decoded.front() == 0xfeff)
        decoded.erase(0, 1);
    for (auto& c : decoded) {
        if (c == L'\r' || c == L'\v' || c == L'\f' || (c >= 0x1c && c <= 0x1e) || c == 0x85 || c == 0x2028 ||
            c == 0x2029)
            c = L'\n';
    }
    std::wistringstream lines(decoded);
    // 未知节中的路径不参与同步或保护规则。
    enum class Section { Managed, Preserve, Other };
    auto section = Section::Other;
    bool hasManagedSection = false;
    ParsedManifest result;
    std::wstring raw;
    while (std::getline(lines, raw)) {
        const auto line = Trim(raw);
        if (line.empty())
            continue;
        if (line == L"[managed]") {
            section = Section::Managed;
            hasManagedSection = true;
        } else if (line == L"[preserve]")
            section = Section::Preserve;
        else if (line.front() == L'[')
            section = Section::Other;
        else if (section != Section::Other) {
            if (!IsSafeRelative(line))
                throw std::invalid_argument("Unsafe path in manifest");
            (section == Section::Managed ? result.managed : result.preserve).push_back(NormalizeRel(line));
        }
    }
    if (!hasManagedSection)
        throw std::invalid_argument("Manifest is missing the managed section");
    return result;
}

// 匹配同名路径或目录规则的后代；includeAncestors 允许查询规则路径的祖先目录。
bool MatchesRule(const std::wstring& relative, const std::wstring& rule, bool includeAncestors) {
    if (rule.empty())
        return false;
    const auto normalized = PathKey(relative);
    const auto key = PathKey(rule);
    if (normalized == key)
        return true;
    const bool directory = rule.back() == L'/' || rule.back() == L'\\';
    if (directory && normalized.compare(0, key.size() + 1, key + L'/') == 0)
        return true;
    return includeAncestors && key.compare(0, normalized.size() + 1, normalized + L'/') == 0;
}

}

ManifestSet LoadManifests(const fs::path& extractDir, const fs::path& installDir,
                          const std::vector<std::wstring>& preserveFromArgv) {
    ManifestSet result;
    for (const auto& rule : preserveFromArgv) {
        if (!IsSafeRelative(rule))
            throw std::invalid_argument("Unsafe preserve rule");
        result.preserve.push_back(NormalizeRel(rule));
    }
    try {
        const auto parsed = ParseManifest(SafeChild(extractDir, kManifestFilename));
        result.managed = parsed.managed;
        // 更新包规则不能取消宿主对用户数据的保护。
        result.preserve.insert(result.preserve.end(), parsed.preserve.begin(), parsed.preserve.end());
        LogInfo(L"已加载新版 manifest：" + std::to_wstring(result.managed.size()) + L" managed");
    } catch (const std::exception& error) {
        LogWarning(L"新版清单不可用，跳过旧文件清理: " + ExceptionMessage(error));
        return result;
    }

    try {
        auto baseline = SafeChild(installDir, L"Updater/installed.manifest");
        if (!fs::exists(baseline))
            baseline = SafeChild(installDir, kManifestFilename);
        result.installed = ParseManifest(baseline).managed;
        LogInfo(L"已加载旧版基线：" + std::to_wstring(result.installed.size()) + L" managed");
    } catch (const std::exception& error) {
        LogWarning(L"旧版基线不可用，跳过旧文件清理: " + ExceptionMessage(error));
    }
    return result;
}

bool IsLanguageConfig(const std::wstring& relativePath) {
    return MatchesRule(relativePath, L"config/languages/", false);
}

bool IsPreserved(const ManifestSet& manifests, const std::wstring& relativePath) {
    for (const auto* rule : {L"data/", L"config/", L"cache/", L"logs/", L"plugins/", L"Updater/"}) {
        if (MatchesRule(relativePath, rule, false))
            return true;
    }
    for (const auto& rule : manifests.preserve) {
        if (MatchesRule(relativePath, rule, false))
            return true;
    }
    return false;
}

bool IsInManifest(const ManifestSet& manifests, const std::wstring& relativePath) {
    const bool directory = !relativePath.empty() && (relativePath.back() == L'/' || relativePath.back() == L'\\');
    for (const auto& rule : manifests.installed) {
        if (IsSafeRelative(rule) && MatchesRule(relativePath, rule, directory))
            return true;
    }
    return false;
}

}
