#pragma once

#include "manifest.h"
#include <functional>

namespace updater {

// 扫描时生成的相对路径计划；不持有文件句柄，执行时仍须重新校验路径与文件状态。
struct SyncPlan {
    std::vector<std::wstring> toCopy;
    std::vector<std::wstring> toRemove;
    std::vector<std::wstring> sourceDirs;
    std::vector<std::wstring> newFiles; // 同步前不存在的目标，用于回滚。
};

// 每项复制或删除尝试前同步调用，current 从 1 开始，total 为两类操作数之和。
// description 仅在调用期间有效；回调在调用线程执行，应避免抛出异常。
using ProgressFn = std::function<void(size_t current, size_t total, const std::wstring& description)>;

// 校验目录并扫描包内容，跳过受保护目标，语言文件只补缺；删除范围限定为已安装基线。
// 不修改磁盘，非法路径、包内重解析点或枚举失败时抛出异常。
SyncPlan CollectSyncPlan(const ManifestSet& manifests, const std::filesystem::path& source,
                         const std::filesystem::path& target);
// 写入前校验复制与删除计划，再逐文件暂存和替换；校验或任意复制失败返回 false。
// 复制失败时跳过删除，不自动回滚已完成的替换；删除和目录整理失败仅记录警告。
// progress 的异常向调用方传播；返回 true 不保证旧文件及空目录均已清理。
bool SyncFiles(const ManifestSet& manifests, const std::filesystem::path& source, const std::filesystem::path& target,
               const SyncPlan& plan, const ProgressFn& progress = {});
// 在非保护区域内尝试删除 .old_update 文件，跳过重解析点；单次删除失败不影响流程。
void CleanupOldUpdateFiles(const std::filesystem::path& installDir, const ManifestSet& manifests = {});
// 从已有备份逐文件恢复非保护内容，并删除 plan 中无备份的新增非保护文件；不创建备份。
// 仅在至少恢复一个文件且无已记录失败时返回 true，不保证备份完整或恢复为整目录快照。
bool RollbackFromBackup(const std::filesystem::path& backupDir, const std::filesystem::path& installDir,
                        const ManifestSet& manifests = {}, const SyncPlan* plan = nullptr);
// 尝试将包内清单保存为 Updater/installed.manifest，不重新解析内容；缺失时跳过，失败仅记警告。
void SaveInstalledManifest(const std::filesystem::path& installDir, const std::filesystem::path& extractDir);
// 尝试移除整个解压目录，未完全清理时记录警告；调用方须事先确认该目录可删除。
void CleanupExtract(const std::filesystem::path& extractDir);
// 递归移除指定树并清除普通节点只读属性，重解析点按叶节点删除；不存在视为成功。
// 拒绝空路径与盘符根目录，其余删除范围由调用方验证；任意清理失败返回 false。
bool RobustRemoveAll(const std::filesystem::path& path);

}
