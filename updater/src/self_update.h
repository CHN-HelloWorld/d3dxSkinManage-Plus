#pragma once

#include <filesystem>

namespace updater {

// 管理一次更新器映像替换及旧映像恢复，独立于主程序备份；对象限同一线程顺序使用。
// 未提交时析构会尝试回滚，恢复失败保留材料供后续处理，不保证进程崩溃后的自动恢复。
class UpdaterReplacement {
  public:
    // 建立尚未发布映像的事务对象。
    UpdaterReplacement() = default;
    // 未提交时尽力恢复旧更新器，已提交时不再修改文件。
    ~UpdaterReplacement();
    // 禁止复制，防止多个对象重复处理同一组替换文件。
    UpdaterReplacement(const UpdaterReplacement&) = delete;
    // 禁止复制赋值，保持恢复责任唯一。
    UpdaterReplacement& operator=(const UpdaterReplacement&) = delete;

    // 校验目录和 x64 PE 结构，将新映像暂存后发布，并保留改名后的旧映像。
    // 包内没有更新器也返回 true；失败记录日志、尝试回滚并返回 false，不得复用已提交对象。
    bool Install(const std::filesystem::path& extractDir, const std::filesystem::path& installDir);
    // 确认主程序同步成功，取消后续回滚；旧映像留待退出后清理，不检查主程序启动结果。
    void Commit() noexcept;
    // 未提交时移除已发布映像并恢复旧映像；无操作或恢复完成返回 true，失败保留状态并返回 false。
    bool Rollback() noexcept;

  private:
    std::filesystem::path destination_;
    std::filesystem::path retired_;
    bool published_ = false;
    bool committed_ = false;
};

// 仅在当前更新器有效时，清理 Updater 直属目录内名称和 PE 结构均符合要求的旧映像。
// 跳过重解析点，不递归；删除失败的映像保留供下次重试，异常只记录警告。
void CleanupRetiredUpdaters(const std::filesystem::path& installDir);

}
