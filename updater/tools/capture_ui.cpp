// 仅用于离屏界面预览，不链接更新工作流程。
#include "../src/ui_render.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <vector>

namespace fs = std::filesystem;
using updater::UpdaterVisualState;

// 将固定预览文案转为 UTF-8 JSON 字符串，转义引号、反斜杠及换行。
// 仅用于本工具生成的文本，不处理任意控制字符；编码转换无法取得长度时生成空字符串。
std::string Json(const std::wstring& text) {
    const int count =
        WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
    std::string utf8(static_cast<size_t>(count), '\0');
    if (count)
        WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), utf8.data(), count, nullptr,
                            nullptr);
    std::string quoted = "\"";
    for (const char character : utf8) {
        if (character == '\\' || character == '"')
            quoted += '\\';
        if (character == '\n')
            quoted += "\\n";
        else if (character == '\r')
            quoted += "\\r";
        else
            quoted += character;
    }
    return quoted + '"';
}

// 在 96 DPI 下将借用的状态绘制为自顶向下的 32 位 BMP，不创建可见窗口。
// 正常路径释放本次创建的 GDI 资源；位图、绘图上下文创建或写入失败时返回 false，可能留下部分文件。
bool Capture(const fs::path& path, const UpdaterVisualState& state) {
    constexpr int width = updater::kUpdaterClientWidth, height = updater::kUpdaterClientHeight;
    BITMAPINFO info{};
    info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    info.bmiHeader.biWidth = width;
    info.bmiHeader.biHeight = -height;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    const HDC dc = CreateCompatibleDC(nullptr);
    void* pixels = nullptr;
    const HBITMAP bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &pixels, nullptr, 0);
    if (!dc || !bitmap || !pixels) {
        if (bitmap)
            DeleteObject(bitmap);
        if (dc)
            DeleteDC(dc);
        return false;
    }
    const auto previous = SelectObject(dc, bitmap);
    const HFONT title = updater::CreateUpdaterFont(96, 15, FW_SEMIBOLD);
    const HFONT step = updater::CreateUpdaterFont(96, 10, FW_NORMAL);
    const HFONT detail = updater::CreateUpdaterFont(96, 9, FW_NORMAL);
    updater::DrawUpdater(dc, 96, state, title, step, detail);
    GdiFlush();
    BITMAPFILEHEADER header{};
    header.bfType = 0x4d42;
    header.bfOffBits = sizeof(header) + sizeof(info.bmiHeader);
    header.bfSize = header.bfOffBits + width * height * 4;
    std::ofstream output(path, std::ios::binary);
    output.write(reinterpret_cast<const char*>(&header), sizeof(header));
    output.write(reinterpret_cast<const char*>(&info.bmiHeader), sizeof(info.bmiHeader));
    output.write(static_cast<const char*>(pixels), width * height * 4);
    const bool ok = static_cast<bool>(output);
    SelectObject(dc, previous);
    DeleteObject(title);
    DeleteObject(step);
    DeleteObject(detail);
    DeleteObject(bitmap);
    DeleteDC(dc);
    return ok;
}

// 向指定目录写入预设状态的 BMP 和 frames.json；参数错误返回 2，捕获或清单写入失败返回 1。
// 目录操作异常向外传播；图标从当前工作目录读取，缺失时仍可绘制不含图标的预览。
int wmain(int argc, wchar_t** argv) {
    if (argc != 2) {
        std::cerr << "Usage: capture_ui OUTPUT_DIRECTORY\n";
        return 2;
    }
    const fs::path directory = fs::absolute(argv[1]);
    fs::create_directories(directory);
    const HICON appIcon =
        static_cast<HICON>(LoadImageW(nullptr, L"resources/app.ico", IMAGE_ICON, 16, 16, LR_LOADFROMFILE));
    const std::array<std::wstring, 4> names = {L"等待主程序退出", L"同步更新文件", L"启动新版本", L"清理临时文件"};
    std::vector<std::pair<std::wstring, UpdaterVisualState>> frames;
    // 组装固定预览帧并复制文案，所有帧借用同一个图标；step 须处于 0 至 4。
    const auto add = [&](const std::wstring& name, int step, const std::wstring& detail, double progress,
                         bool busy = false, int result = -1) {
        UpdaterVisualState state;
        state.caption = L"d3dxSkinManage-Plus 更新程序";
        state.appIcon = appIcon;
        state.title = result == 1 ? L"更新完成" : result == 0 ? L"更新遇到问题" : L"正在安装更新";
        state.step = step ? L"步骤 " + std::to_wstring(step) + L"/4: " + names[step - 1] + L"…" : L"准备中…";
        if (result >= 0)
            state.step = result ? L"更新已完成，此窗口即将关闭。" : L"更新出错，请查看 logs/ 下的日志";
        state.detail = result < 0 ? detail : L"";
        state.footer = L"此窗口将在更新结束后自动关闭。";
        state.steps = {L"等待退出", L"替换文件", L"启动新版", L"清理文件"};
        state.currentStep = step;
        state.result = result;
        state.percentage = progress;
        state.indeterminate = busy;
        state.showPercentage = result == 1 || (result < 0 && !busy && step > 1);
        frames.push_back({L"native-" + name, state});
    };
    add(L"prepare", 0, L"", 0);
    add(L"wait", 1, L"正在等待主程序退出…", 0, true);
    add(L"analyze", 2, L"分析文件差异…", 0);
    add(L"self-update", 2, L"正在更新更新器…", 0);
    add(L"retry", 2, L"替换: d3dxSkinManage-Plus.exe", 35);
    add(L"rollback", 2, L"正在从备份回滚…", 62);
    add(L"restored", 3, L"同步失败已回滚，跳过启动", 62);
    add(L"launch", 3, L"d3dxSkinManage-Plus.exe", 100, true);
    add(L"cleanup", 4, L"清理临时文件…", 40);
    const std::array<std::wstring, 6> files = {L"d3dxSkinManage-Plus.exe",     L"python312.dll",
                                               L"PySide6/Qt6Core.dll",         L"PySide6/Qt6Widgets.dll",
                                               L"resources/themes/fluent.qss", L"resources/app.ico"};
    for (int percent = 0; percent <= 100; percent += 5)
        add(L"copy-" + std::to_wstring(percent), 2, L"替换: " + files[std::min(5, percent / 18)], percent);
    add(L"remove", 2, L"清理: resources/obsolete-resource.dat", 96);
    add(L"success", 4, L"", 100, false, 1);
    add(L"error", 3, L"", 62, false, 0);
    add(L"early-error", 0, L"", 0, false, 0);
    add(L"launch-error", 3, L"", 100, false, 0);
    std::ofstream manifest(directory / L"frames.json", std::ios::binary);
    manifest << "{\"width\":" << updater::kUpdaterClientWidth << ",\"height\":" << updater::kUpdaterClientHeight
             << ",\"full_frame\":true,\"caption_height\":" << updater::kUpdaterCaptionHeight << ",\"bar\":[26,"
             << 282 + updater::kUpdaterCaptionHeight << ",568,6],\"frames\":[";
    bool first = true;
    for (const auto& [name, state] : frames) {
        const std::wstring file = name + L".bmp";
        if (!Capture(directory / file, state)) {
            std::cerr << "Capture failed\n";
            return 1;
        }
        if (!first)
            manifest << ',';
        first = false;
        manifest << "{\"name\":" << Json(name) << ",\"file\":" << Json(file)
                 << ",\"busy\":" << (state.indeterminate ? "true" : "false") << ",\"labels\":[" << Json(state.title)
                 << ',' << Json(state.step) << ',' << Json(state.detail) << "]}";
    }
    manifest << "]}";
    if (appIcon)
        DestroyIcon(appIcon);
    std::cout << "Captured " << frames.size() << " native frames without running an update.\n";
    return manifest ? 0 : 1;
}
