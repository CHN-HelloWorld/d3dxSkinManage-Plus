#pragma once

#include <windows.h>
#include <filesystem>
#include <functional>
#include <string>
#include <vector>

namespace updater {

// 一次同步更新的输入；调用期间保持有效，备份由主程序预先准备，更新器不创建备份。
struct UpdateRequest {
    DWORD pid = 0;
    std::filesystem::path extractDir;
    std::filesystem::path installDir;
    std::vector<std::wstring> preserve;
    DWORD waitTimeoutMs = 30000;
};

// 可选的进度通知，均在 RunUpdate 的调用线程同步执行；回调应不抛出异常。
// 字符串引用只在回调期间有效，跨线程界面实现须复制内容并通过自身消息机制更新控件。
struct UpdateCallbacks {
    // 步骤编号为 1 至 4，依次表示等待、同步、启动和清理。
    std::function<void(int)> step;
    // 当前操作或错误说明。
    std::function<void(const std::wstring&)> detail;
    // 当前步骤的百分比，不表示跨步骤单调递增的总进度。
    std::function<void(double)> progress;
    // 切换无法估算完成比例时的动画状态。
    std::function<void(bool)> indeterminate;
};

// 同步执行等待退出、文件更新、启动与清理；通常由工作线程调用，函数本身不创建线程。
// 同步失败时尝试恢复现有备份及旧更新器；同步成功即提交更新器，启动失败不再回滚。
// 返回 true 表示同步和启动请求成功，清理及清单保存失败可仅记警告；不执行启动后健康检查。
bool RunUpdate(const UpdateRequest& request, const UpdateCallbacks& callbacks = {});

}
