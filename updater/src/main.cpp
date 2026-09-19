#include "language.h"
#include "logger.h"
#include "paths.h"
#include "token.h"
#include "ui.h"
#include "util.h"
#include "workflow.h"

#include <algorithm>
#include <limits>
#include <shellapi.h>
#include <stdexcept>
#include <system_error>
#include <thread>

namespace updater {
namespace {
// 在进入更新流程前显示模态错误框，并返回统一的初始化失败退出码 1。
int ShowError(const std::wstring& message) {
    const auto title = Tr(L"d3dxSkinManage-Plus 更新器");
    MessageBoxW(nullptr, message.c_str(), title.c_str(), MB_OK | MB_ICONERROR);
    return 1;
}

// 校验启动参数后建立日志、语言及界面；通常在工作线程更新，主线程处理窗口消息。
// 无窗口或线程创建失败时同步执行；进入工作流程后返回 0，更新结果由界面和日志报告。
int RunUpdater() {
    int argc = 0;
    // 接管 CommandLineToArgvW 一次分配的参数数组及其中字符串。
    struct Arguments {
        wchar_t** data;
        // 参数使用完毕后通过 LocalFree 释放整块分配。
        ~Arguments() {
            if (data)
                LocalFree(data);
        }
    } argv{CommandLineToArgvW(GetCommandLineW(), &argc)};
    if (!argv.data || argc != 6 || !VerifyLaunchToken(argv.data[5])) {
        return ShowError(Tr(L"此程序由 d3dxSkinManage-Plus 自动调用，不支持手动运行。"));
    }

    UpdateRequest request;
    try {
        const std::wstring pidText = argv.data[1];
        if (pidText.empty() ||
            !std::all_of(pidText.begin(), pidText.end(), [](wchar_t c) { return c >= L'0' && c <= L'9'; }))
            throw std::invalid_argument("Invalid process ID");
        const auto value = std::stoull(pidText);
        if (!value || value > (std::numeric_limits<DWORD>::max)() || value == GetCurrentProcessId()) {
            throw std::invalid_argument("Invalid process ID");
        }
        request.pid = static_cast<DWORD>(value);
        request.extractDir = std::filesystem::absolute(argv.data[2]).lexically_normal();
        request.installDir = std::filesystem::absolute(argv.data[3]).lexically_normal();
        request.preserve = ParseJsonStringArray(argv.data[4]);
        ValidateUpdateDirectories(request.extractDir, request.installDir);
        if (!std::filesystem::is_directory(request.installDir)) {
            return ShowError(Tr(L"安装目录不存在: {path}", {{L"path", request.installDir.wstring()}}));
        }
    } catch (const std::exception& error) {
        return ShowError(Tr(L"参数错误: {error}", {{L"error", ExceptionMessage(error)}}));
    }

    InitLog(request.installDir);
    InitializeLanguage(request.installDir);
    LogInfo(L"PID=" + std::to_wstring(request.pid) + L"，解压=" + request.extractDir.wstring() + L"，安装=" +
            request.installDir.wstring());
    UpdaterUI ui;
    const bool hasWindow = ui.Create();
    UpdateCallbacks callbacks;
    if (hasWindow) {
        // 回调只调用可跨线程的界面状态接口；控件操作由窗口消息线程完成。
        callbacks.step = [&](int step) { ui.SetStep(step); };
        callbacks.detail = [&](const std::wstring& text) { ui.SetDetail(text); };
        callbacks.progress = [&](double progress) { ui.SetProgress(progress); };
        callbacks.indeterminate = [&](bool active) {
            if (active)
                ui.SetIndeterminate();
            else
                ui.StopIndeterminate();
        };
    } else
        LogWarning(L"无法创建进度窗口，继续在后台更新");

    // 借用请求与界面直到线程汇合，执行更新后关闭日志并请求显示结果及延迟关窗。
    const auto worker = [&] {
        const bool success = RunUpdate(request, callbacks);
        LogInfo(success ? L"更新成功" : L"更新失败，请保留备份并查看日志");
        CloseLog();
        if (hasWindow) {
            ui.ShowResult(success);
            ui.CloseAfter(success ? 2000 : 5000);
        }
    };
    if (!hasWindow) {
        worker();
        return 0;
    }
    std::thread thread;
    try {
        thread = std::thread(worker);
    } catch (const std::system_error& error) {
        LogWarning(L"无法创建工作线程: " + ExceptionMessage(error));
        worker();
    }
    ui.Run();
    if (thread.joinable())
        thread.join();
    // 工作流程的结果由界面和日志报告。
    return 0;
}
}
}

// Windows GUI 入口；将未处理的初始化异常转换为错误框和退出码 1。
int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    try {
        return updater::RunUpdater();
    } catch (const std::exception& error) {
        return updater::ShowError(updater::ExceptionMessage(error));
    } catch (...) {
        return updater::ShowError(L"Updater failed to initialize.");
    }
}
