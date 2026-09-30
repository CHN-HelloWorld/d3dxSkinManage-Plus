#include "sync.h"
#include "language.h"
#include "logger.h"
#include "paths.h"
#include "util.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <iterator>
#include <map>
#include <set>
#include <stdexcept>
#include <system_error>

namespace updater {
namespace {
namespace fs = std::filesystem;

// 将文件和父目录不存在统一视为已删除，用于幂等清理。
bool IsMissing(DWORD error) {
    return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND;
}

// 尝试清除只读属性并删除本次创建的暂存文件；调用方负责所有权，清理失败不抛出异常。
void RemoveOwnedFile(const fs::path& path) {
    const DWORD attributes = GetFileAttributesW(path.c_str());
    if (attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_READONLY)) {
        SetFileAttributesW(path.c_str(), attributes & ~FILE_ATTRIBUTE_READONLY);
    }
    DeleteFileW(path.c_str());
}

// 拥有尚未发布的暂存文件；发布成功后由调用方清空 path 解除清理责任。
struct StagedFile {
    fs::path path;
    // 在正常返回或异常退出时尽力删除未发布内容。
    ~StagedFile() {
        if (!path.empty())
            RemoveOwnedFile(path);
    }
};

// 先重试删除目标，占用持续时将其移到 .old_update；改名成功后通过 renamed 返回恢复路径。
// 无法改名或清除旧残留时返回 false，其他删除错误抛出异常；调用方须传入已校验路径。
bool RemoveForReplacement(const fs::path& path, const std::wstring& relative, fs::path& renamed) {
    if (!fs::exists(path))
        return true;
    for (unsigned attempt = 0; attempt < 3; ++attempt) {
        if (DeleteFileW(path.c_str()))
            return true;
        const DWORD error = GetLastError();
        if (IsMissing(error))
            return true;
        if (error != ERROR_ACCESS_DENIED && error != ERROR_SHARING_VIOLATION && error != ERROR_LOCK_VIOLATION) {
            throw std::system_error(static_cast<int>(error), std::system_category(), "DeleteFileW");
        }
        if (attempt < 2) {
            LogWarning(L"文件被占用，重试 (" + std::to_wstring(attempt + 1) + L"/3): " + relative);
            Sleep(1000);
        }
    }
    auto backup = path;
    backup += L".old_update";
    if (fs::exists(backup) && !DeleteFileW(backup.c_str())) {
        LogError(L"无法清理旧替换文件: " + relative + L": " + Win32Error(GetLastError()));
        return false;
    }
    if (!MoveFileW(path.c_str(), backup.c_str())) {
        LogError(L"替换失败: " + relative + L": " + Win32Error(GetLastError()));
        return false;
    }
    renamed = std::move(backup);
    return true;
}

// 完整复制到目标同目录的独占暂存文件后再腾出目标并发布；路径安全由调用方预先校验。
// 腾出目标失败返回 false，复制或发布失败抛出异常；发布失败只尝试恢复已改名的旧文件。
// 已直接删除的旧文件须依靠外部备份恢复，此操作不提供整次更新的原子性。
bool ReplaceFile(const fs::path& source, const fs::path& destination, const std::wstring& relative) {
    static std::atomic<unsigned long long> serial{0};
    fs::create_directories(destination.parent_path());
    StagedFile staged;
    // 先写入暂存文件，避免读取错误或磁盘已满时破坏现有文件。
    for (unsigned attempt = 0; attempt < 8; ++attempt) {
        auto temporary = destination;
        temporary += L".new_update." + std::to_wstring(GetCurrentProcessId()) + L"." +
                     std::to_wstring(GetTickCount64()) + L"." + std::to_wstring(serial++);
        if (CopyFileW(source.c_str(), temporary.c_str(), TRUE)) {
            staged.path = std::move(temporary);
            break;
        }
        const DWORD error = GetLastError();
        if (error != ERROR_FILE_EXISTS && error != ERROR_ALREADY_EXISTS) {
            // CopyFile 读取或写入失败后可能留下不完整文件。
            RemoveOwnedFile(temporary);
            throw std::system_error(static_cast<int>(error), std::system_category(), "CopyFileW");
        }
    }
    if (staged.path.empty())
        throw std::runtime_error("Cannot allocate a staging filename");
    fs::path renamed;
    if (!RemoveForReplacement(destination, relative, renamed))
        return false;
    if (!MoveFileExW(staged.path.c_str(), destination.c_str(), MOVEFILE_WRITE_THROUGH)) {
        const DWORD error = GetLastError();
        if (!renamed.empty())
            MoveFileW(renamed.c_str(), destination.c_str());
        throw std::system_error(static_cast<int>(error), std::system_category(), "MoveFileExW");
    }
    staged.path.clear();
    return true;
}

// 以 CREATE_NEW 补齐语言文件，目标已存在时保留原内容；读写或刷新失败抛出异常并清理半成品。
// 时间戳复制为尽力操作，源与目标路径须由调用方先行校验。
void CopyMissingLanguage(const fs::path& source, const fs::path& destination) {
    fs::create_directories(destination.parent_path());
    // 独占管理源文件句柄，复制期间仅允许其他读取者共享。
    struct Input {
        HANDLE handle;
        // 无论复制是否完成均关闭源句柄。
        ~Input() {
            if (handle != INVALID_HANDLE_VALUE)
                CloseHandle(handle);
        }
    } input{CreateFileW(source.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
                        nullptr)};
    if (input.handle == INVALID_HANDLE_VALUE) {
        throw std::system_error(static_cast<int>(GetLastError()), std::system_category(), "Open language source");
    }
    // 拥有新建目标及其句柄；path 借用函数参数，complete 表示内容已刷新。
    struct Output {
        HANDLE handle;
        const fs::path& path;
        bool complete = false;
        // 先关闭句柄，未完成时再尽力删除本次创建的文件。
        ~Output() {
            if (handle != INVALID_HANDLE_VALUE) {
                CloseHandle(handle);
                if (!complete)
                    DeleteFileW(path.c_str());
            }
        }
    } output{CreateFileW(destination.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr),
             destination};
    if (output.handle == INVALID_HANDLE_VALUE) {
        const DWORD error = GetLastError();
        if (error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS)
            return;
        throw std::system_error(static_cast<int>(error), std::system_category(), "Create language configuration");
    }
    std::array<char, 65536> buffer{};
    for (;;) {
        DWORD read = 0;
        if (!ReadFile(input.handle, buffer.data(), static_cast<DWORD>(buffer.size()), &read, nullptr)) {
            throw std::system_error(static_cast<int>(GetLastError()), std::system_category(),
                                    "Read language configuration");
        }
        if (!read)
            break;
        DWORD offset = 0;
        while (offset < read) {
            DWORD written = 0;
            if (!WriteFile(output.handle, buffer.data() + offset, read - offset, &written, nullptr) || !written) {
                throw std::runtime_error("Cannot write language configuration");
            }
            offset += written;
        }
    }
    FILETIME created{}, accessed{}, modified{};
    if (GetFileTime(input.handle, &created, &accessed, &modified)) {
        SetFileTime(output.handle, &created, &accessed, &modified);
    }
    if (!FlushFileBuffers(output.handle)) {
        throw std::system_error(static_cast<int>(GetLastError()), std::system_category(),
                                "Flush language configuration");
    }
    output.complete = true;
}

// 遍历非保护且非重解析点的后代，在调用线程向 callback 传递目录项与相对路径。
// 根不存在时无操作；不验证根的祖先，枚举及回调异常交由调用方处理。
template <class Callback> void WalkInstalled(const fs::path& root, const ManifestSet& manifests, Callback callback) {
    if (!fs::exists(root))
        return;
    fs::recursive_directory_iterator it(root);
    const fs::recursive_directory_iterator end;
    for (; it != end; ++it) {
        const auto relative = it->path().lexically_relative(root).generic_wstring();
        if (IsPreserved(manifests, relative) || IsReparsePoint(it->path())) {
            it.disable_recursion_pending();
            continue;
        }
        callback(*it, relative);
    }
}

// 从深到浅删除旧基线覆盖且新版不再声明的空目录，保留用户目录及包内仍存在的空目录。
void CleanupEmptyDirs(const ManifestSet& manifests, const fs::path& target,
                      const std::vector<std::wstring>& sourceDirectories) {
    std::vector<fs::path> directories;
    WalkInstalled(target, manifests, [&](const fs::directory_entry& entry, const std::wstring&) {
        if (entry.is_directory())
            directories.push_back(entry.path());
    });
    std::sort(directories.begin(), directories.end(), [](const auto& a, const auto& b) {
        return std::distance(a.begin(), a.end()) > std::distance(b.begin(), b.end());
    });
    std::set<std::wstring> sourceDirs;
    for (const auto& relative : sourceDirectories)
        sourceDirs.insert(PathKey(relative));
    for (const auto& directory : directories) {
        const auto relative = directory.lexically_relative(target).generic_wstring();
        if (!IsInManifest(manifests, relative + L'/') || sourceDirs.count(PathKey(relative)))
            continue;
        std::error_code error;
        if (fs::is_empty(directory, error) && !error)
            fs::remove(SafeChild(target, relative), error);
    }
}

// 逐节点删除调用方授权的目录树，不进入重解析点目标；累积枚举、属性修改和删除失败。
bool RemoveTreeClearingReadOnly(const fs::path& path) {
    const DWORD attributes = GetFileAttributesW(path.c_str());
    if (attributes == INVALID_FILE_ATTRIBUTES)
        return IsMissing(GetLastError());
    bool success = true;
    const bool directory = (attributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
    // 重解析点和目录联接按叶节点处理，不遍历其目标。
    if (directory && !(attributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
        std::error_code ec;
        fs::directory_iterator it(path, ec);
        const fs::directory_iterator end;
        for (; !ec && it != end; it.increment(ec)) {
            if (!RemoveTreeClearingReadOnly(it->path()))
                success = false;
        }
        if (ec)
            success = false;
    }
    if ((attributes & FILE_ATTRIBUTE_READONLY) && !(attributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
        DWORD writable = attributes & ~FILE_ATTRIBUTE_READONLY;
        if (!writable)
            writable = FILE_ATTRIBUTE_NORMAL;
        if (!SetFileAttributesW(path.c_str(), writable))
            success = false;
    }
    const BOOL removed = directory ? RemoveDirectoryW(path.c_str()) : DeleteFileW(path.c_str());
    return removed && success;
}
}

SyncPlan CollectSyncPlan(const ManifestSet& manifests, const fs::path& source, const fs::path& target) {
    ValidateUpdateDirectories(source, target);
    SyncPlan plan;
    std::map<std::wstring, std::wstring> sourceFiles;
    std::set<std::wstring> sourceDirs;
    for (const auto& entry : fs::recursive_directory_iterator(source)) {
        const auto relative = entry.path().lexically_relative(source).generic_wstring();
        if (!IsSafeRelative(relative) || IsReparsePoint(entry.path())) {
            throw std::invalid_argument("Package contains an unsafe path or reparse point");
        }
        if (entry.is_regular_file())
            sourceFiles.emplace(PathKey(relative), relative);
        else if (entry.is_directory())
            sourceDirs.insert(relative);
    }
    for (const auto& item : sourceFiles) {
        const auto& relative = item.second;
        if (item.first == PathKey(kManifestFilename))
            continue;
        const bool language = IsLanguageConfig(relative);
        if (IsPreserved(manifests, relative) && !language)
            continue;
        const auto destination = SafeChild(target, relative);
        const bool exists = fs::exists(destination);
        if (language && exists)
            continue;
        plan.toCopy.push_back(relative);
        if (!exists)
            plan.newFiles.push_back(relative);
    }
    plan.sourceDirs.assign(sourceDirs.begin(), sourceDirs.end());
    WalkInstalled(target, manifests, [&](const fs::directory_entry& entry, const std::wstring& relative) {
        if (entry.is_regular_file() && PathKey(relative) != PathKey(kManifestFilename) &&
            !sourceFiles.count(PathKey(relative)) && IsInManifest(manifests, relative)) {
            plan.toRemove.push_back(relative);
        }
    });
    std::sort(plan.toRemove.begin(), plan.toRemove.end());
    return plan;
}

bool SyncFiles(const ManifestSet& manifests, const fs::path& source, const fs::path& target, const SyncPlan& plan,
               const ProgressFn& progress) {
    try {
        ValidateUpdateDirectories(source, target);
        // 首次写入前校验完整执行计划。
        for (const auto& relative : plan.toCopy) {
            if (PathKey(relative) == PathKey(kManifestFilename) ||
                (IsPreserved(manifests, relative) && !IsLanguageConfig(relative))) {
                throw std::invalid_argument("Copy plan contains a preserved path");
            }
            if (!fs::is_regular_file(SafeChild(source, relative))) {
                throw std::invalid_argument("Copy source is missing or not a file");
            }
            const auto destination = SafeChild(target, relative);
            if (fs::exists(destination) && !fs::is_regular_file(destination)) {
                throw std::invalid_argument("File destination is not a regular file");
            }
        }
        for (const auto& relative : plan.toRemove) {
            SafeChild(target, relative);
            if (IsPreserved(manifests, relative) || !IsInManifest(manifests, relative) ||
                PathKey(relative) == PathKey(kManifestFilename)) {
                throw std::invalid_argument("Removal plan contains an unmanaged or preserved path");
            }
        }
    } catch (const std::exception& error) {
        LogError(L"同步计划无效: " + ExceptionMessage(error));
        return false;
    }

    const size_t total = plan.toCopy.size() + plan.toRemove.size();
    size_t current = 0;
    size_t errors = 0;
    for (const auto& relative : plan.toCopy) {
        ++current;
        if (progress)
            progress(current, total, Tr(L"替换: {rel}", {{L"rel", relative}}));
        try {
            const auto destination = SafeChild(target, relative);
            const auto input = SafeChild(source, relative);
            if (IsLanguageConfig(relative))
                CopyMissingLanguage(input, destination);
            else if (!ReplaceFile(input, destination, relative)) {
                ++errors;
                continue;
            }
            LogInfo(L"[替换/补齐] " + relative);
        } catch (const std::exception& error) {
            ++errors;
            LogError(L"[替换失败] " + relative + L": " + ExceptionMessage(error));
        }
    }
    if (errors) {
        LogError(L"复制失败，跳过废弃文件清理");
        return false;
    }

    for (const auto& relative : plan.toRemove) {
        ++current;
        if (progress)
            progress(current, total, Tr(L"清理: {rel}", {{L"rel", relative}}));
        try {
            const auto destination = SafeChild(target, relative);
            if (DeleteFileW(destination.c_str()))
                LogInfo(L"[清理] " + relative);
            else if (!IsMissing(GetLastError()))
                LogWarning(L"[清理失败] " + relative + L": " + Win32Error(GetLastError()));
        } catch (const std::exception& error) {
            LogWarning(L"[清理失败] " + relative + L": " + ExceptionMessage(error));
        }
    }
    for (const auto& relative : plan.sourceDirs) {
        if (IsPreserved(manifests, relative))
            continue;
        try {
            fs::create_directories(SafeChild(target, relative));
        } catch (const std::exception& error) {
            LogWarning(L"创建空目录失败: " + ExceptionMessage(error));
        }
    }
    try {
        CleanupEmptyDirs(manifests, target, plan.sourceDirs);
    } catch (const std::exception& error) {
        LogWarning(L"清理空目录失败: " + ExceptionMessage(error));
    }
    LogInfo(L"同步完成：复制=" + std::to_wstring(plan.toCopy.size()) + L"，清理=" +
            std::to_wstring(plan.toRemove.size()));
    return true;
}

void CleanupOldUpdateFiles(const fs::path& installDir, const ManifestSet& manifests) {
    try {
        WalkInstalled(installDir, manifests, [&](const fs::directory_entry& entry, const std::wstring& relative) {
            if (entry.is_regular_file() && PathKey(entry.path().extension().wstring()) == L".old_update") {
                DeleteFileW(SafeChild(installDir, relative).c_str());
            }
        });
    } catch (const std::exception& error) {
        LogWarning(L"旧替换文件清理未完成: " + ExceptionMessage(error));
    }
}

bool RollbackFromBackup(const fs::path& backupDir, const fs::path& installDir, const ManifestSet& manifests,
                        const SyncPlan* plan) {
    size_t restored = 0;
    size_t failed = 0;
    try {
        ValidateUpdateDirectories(backupDir, installDir);
        // 逐文件恢复；不完整备份不能删除无关用户文件或额外保护的子目录。

        WalkInstalled(backupDir, manifests, [&](const fs::directory_entry& entry, const std::wstring& relative) {
            if (!entry.is_regular_file())
                return;
            try {
                if (ReplaceFile(SafeChild(backupDir, relative), SafeChild(installDir, relative), relative))
                    ++restored;
                else
                    ++failed;
            } catch (const std::exception& error) {
                ++failed;
                LogError(L"[回滚失败] " + relative + L": " + ExceptionMessage(error));
            }
        });
        if (plan) {
            for (const auto& relative : plan->newFiles) {
                if (IsPreserved(manifests, relative))
                    continue;
                try {
                    if (fs::exists(SafeChild(backupDir, relative)))
                        continue;
                    const auto path = SafeChild(installDir, relative);
                    if (!DeleteFileW(path.c_str()) && !IsMissing(GetLastError()))
                        ++failed;
                } catch (const std::exception& error) {
                    ++failed;
                    LogError(L"[回滚新增文件失败] " + relative + L": " + ExceptionMessage(error));
                }
            }
        }
    } catch (const std::exception& error) {
        ++failed;
        LogError(L"回滚失败: " + ExceptionMessage(error));
    }
    LogInfo(L"回滚完成：还原=" + std::to_wstring(restored) + L"，失败=" + std::to_wstring(failed));
    return failed == 0 && restored > 0;
}

void SaveInstalledManifest(const fs::path& installDir, const fs::path& extractDir) {
    try {
        const auto source = SafeChild(extractDir, kManifestFilename);
        if (!fs::is_regular_file(source))
            return;
        const auto destination = SafeChild(installDir, L"Updater/installed.manifest");
        if (!ReplaceFile(source, destination, L"Updater/installed.manifest")) {
            LogWarning(L"保存 installed.manifest 失败");
        }
    } catch (const std::exception& error) {
        LogWarning(L"保存 installed.manifest 失败: " + ExceptionMessage(error));
    }
}

void CleanupExtract(const fs::path& extractDir) {
    if (!RobustRemoveAll(extractDir))
        LogWarning(L"解压目录未完全清理: " + extractDir.wstring());
}

bool RobustRemoveAll(const fs::path& path) {
    try {
        if (path.empty() || fs::absolute(path).lexically_normal() == fs::absolute(path).root_path())
            return false;
        return RemoveTreeClearingReadOnly(fs::absolute(path).lexically_normal());
    } catch (const std::exception& error) {
        LogWarning(L"清理目录失败: " + ExceptionMessage(error));
        return false;
    }
}
}
