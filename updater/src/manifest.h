#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace updater {

inline constexpr wchar_t kManifestFilename[] = L"files.manifest";
inline constexpr wchar_t kMainExeName[] = L"d3dxSkinManage-Plus.exe";

// 同步所需的新清单、已安装基线和追加保护规则；路径按 Windows 规则匹配。
struct ManifestSet {
    // 新版声明的受管路径；实际复制内容仍由解压目录扫描决定。
    std::vector<std::wstring> managed;
    // 旧版受管路径，限定允许清理的文件与目录范围。
    std::vector<std::wstring> installed;
    // 命令行与新版清单追加的规则，不覆盖内置用户目录保护。
    std::vector<std::wstring> preserve;
};

// 合并保护规则并读取新版及旧版清单；命令行规则不安全时抛出异常。
// 新版不可读时不读取旧版，旧版不可读时 installed 为空，两者均禁用旧文件清理并记录警告。
ManifestSet LoadManifests(const std::filesystem::path& extractDir, const std::filesystem::path& installDir,
                          const std::vector<std::wstring>& preserveFromArgv);
// 判断路径是否命中内置或追加保护规则；目录规则也保护后代，比较忽略大小写。
bool IsPreserved(const ManifestSet& manifests, const std::wstring& relativePath);
// 查询已安装基线；查询以斜杠结尾的目录时，也匹配其中已登记文件的祖先目录。
bool IsInManifest(const ManifestSet& manifests, const std::wstring& relativePath);
// 判断是否为 config/languages 本身或其后代，用于只补齐缺失语言文件的例外规则。
bool IsLanguageConfig(const std::wstring& relativePath);

}
