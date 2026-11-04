#include "workflow.h"
#include "language.h"
#include "logger.h"
#include "manifest.h"
#include "paths.h"
#include "proc.h"
#include "self_update.h"
#include "sync.h"
#include "util.h"

#include <chrono>
#include <stdexcept>

namespace updater {
namespace fs = std::filesystem;

bool RunUpdate(const UpdateRequest& request, const UpdateCallbacks& callbacks) {
    ManifestSet manifests;
    SyncPlan plan;
    bool syncStarted = false;
    bool syncFinished = false;
    fs::path backup;
    UpdaterReplacement updaterReplacement;
    // 以下转发器允许无界面调用，不改变回调的线程或参数生命周期。
    const auto step = [&](int number) {
        if (callbacks.step)
            callbacks.step(number);
    };
    const auto detail = [&](const std::wstring& text) {
        if (callbacks.detail)
            callbacks.detail(text);
    };
    const auto progress = [&](double value) {
        if (callbacks.progress)
            callbacks.progress(value);
    };
    const auto indeterminate = [&](bool value) {
        if (callbacks.indeterminate)
            callbacks.indeterminate(value);
    };
    // 仅使用主程序已准备的备份恢复，并根据计划移除新增文件；无备份时保留解压材料。
    const auto rollback = [&] {
        try {
            if (!backup.empty() && fs::is_directory(backup)) {
                detail(Tr(L"正在从备份回滚…"));
                return RollbackFromBackup(backup, request.installDir, manifests, &plan);
            }
            LogWarning(L"无备份可回滚，保留解压目录以便恢复");
        } catch (const std::exception& error) {
            LogError(L"回滚失败: " + ExceptionMessage(error));
        }
        return false;
    };

    try {
        ValidateUpdateDirectories(request.extractDir, request.installDir);
        if (!fs::is_directory(request.installDir) ||
            !fs::is_regular_file(SafeChild(request.extractDir, kMainExeName))) {
            throw std::invalid_argument("Update package is missing the main executable or installation directory");
        }
        backup = SafeChild(request.installDir, L"Updater/Cache/backup");
        if (IsWithin(backup, request.extractDir) || IsWithin(request.extractDir, backup)) {
            throw std::invalid_argument("Extraction and recovery directories must not overlap");
        }

        step(1);
        detail(Tr(L"正在等待主程序退出…"));
        indeterminate(true);
        if (!WaitForProcessExit(request.pid, request.waitTimeoutMs))
            return false;

        step(2);
        detail(Tr(L"分析文件差异…"));
        indeterminate(false);
        progress(0);
        manifests = LoadManifests(request.extractDir, request.installDir, request.preserve);
        plan = CollectSyncPlan(manifests, request.extractDir, request.installDir);
        CleanupOldUpdateFiles(request.installDir, manifests);
        CleanupRetiredUpdaters(request.installDir);
        if (fs::exists(SafeChild(request.extractDir, L"Updater/updater.exe"))) {
            detail(Tr(L"正在更新更新器…"));
        }
        if (!updaterReplacement.Install(request.extractDir, request.installDir))
            return false;
        auto lastUpdate = std::chrono::steady_clock::now() - std::chrono::milliseconds(40);
        // 将逐文件进度转换为百分比，最多约每 40 毫秒通知一次，最后一项始终通知。
        const auto onProgress = [&](size_t current, size_t total, const std::wstring& text) {
            const auto now = std::chrono::steady_clock::now();
            if (current == total || now - lastUpdate >= std::chrono::milliseconds(40)) {
                progress(total ? static_cast<double>(current) / static_cast<double>(total) * 100.0 : 0);
                detail(text);
                lastUpdate = now;
            }
        };
        syncStarted = true;
        if (!SyncFiles(manifests, request.extractDir, request.installDir, plan, onProgress)) {
            LogError(L"文件同步失败，开始恢复备份");
            const bool restored = rollback();
            updaterReplacement.Rollback();
            step(3);
            detail(restored ? Tr(L"同步失败已回滚，跳过启动") : Tr(L"更新出错，请查看 logs/ 下的日志"));
            return false;
        }
        syncFinished = true;
        updaterReplacement.Commit();
        // 重启主程序前尝试保存新安装清单；保存失败仅记录警告，不撤销已完成的同步。
        SaveInstalledManifest(request.installDir, request.extractDir);

        step(3);
        detail(kMainExeName);
        indeterminate(true);
        if (!LaunchNewVersion(request.installDir))
            return false;

        step(4);
        indeterminate(false);
        detail(Tr(L"清理临时文件…"));
        progress(40);
        CleanupExtract(request.extractDir);
        if (fs::exists(backup) && !RobustRemoveAll(backup))
            LogWarning(L"旧版本备份未完全清理");
        // 仅清理安装目录内的下载缓存；路径校验或删除失败不改变已完成的更新结果。
        try {
            const auto download = SafeChild(request.installDir, L"Updater/Cache/download");
            if (fs::exists(download)) {
                if (RobustRemoveAll(download))
                    LogInfo(L"已清理更新包下载目录: " + download.wstring());
                else
                    LogWarning(L"更新包下载目录未完全清理: " + download.wstring());
            }
        } catch (const std::exception& error) {
            LogWarning(L"清理更新包下载目录失败: " + ExceptionMessage(error));
        }
        progress(100);
        LogInfo(L"更新完成");
        return true;
    } catch (const std::exception& error) {
        LogError(L"更新过程异常: " + ExceptionMessage(error));
    } catch (...) {
        LogError(L"更新过程发生未知异常");
    }
    if (syncStarted && !syncFinished)
        rollback();
    return false;
}

}
