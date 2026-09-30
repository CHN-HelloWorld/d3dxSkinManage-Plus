#include "ui.h"
#include "../resource.h"
#include "language.h"
#include "logger.h"
#include "ui_render.h"
#include "util.h"

#include <algorithm>
#include <cmath>
#include <dwmapi.h>
#include <memory>
#include <utility>
#include <windowsx.h>

namespace updater {
namespace {

constexpr UINT kUpdateMessage = WM_APP + 1;
constexpr UINT_PTR kAnimationTimer = 1;
constexpr UINT_PTR kCloseTimer = 2;
constexpr wchar_t kWindowClass[] = L"d3dxSkinManagePlus.NativeUpdater.Window";
constexpr DWORD kWindowStyle = WS_POPUP | WS_SYSMENU;

// 按 DPI 设置圆角区域；SetWindowRgn 成功后系统拥有区域，失败时由本函数释放。
void UpdateWindowShape(HWND window, UINT dpi) {
    RECT client{};
    GetClientRect(window, &client);
    const int radius = MulDiv(12, static_cast<int>(dpi), 96);
    const HRGN region = CreateRoundRectRgn(0, 0, client.right + 1, client.bottom + 1, radius, radius);
    if (region && !SetWindowRgn(window, region, TRUE))
        DeleteObject(region);
}

// 判断 UTF-16 高代理项，用于按完整码点截取详情尾部。
bool IsHighSurrogate(wchar_t c) {
    return c >= 0xd800 && c <= 0xdbff;
}
// 判断 UTF-16 低代理项，避免截断配对字符。
bool IsLowSurrogate(wchar_t c) {
    return c >= 0xdc00 && c <= 0xdfff;
}

// 详情最多显示 55 个码点；超长时保留尾部 52 个，优先露出文件名且不拆开代理对。
std::wstring ShortDetail(const std::wstring& text) {
    size_t characters = 0;
    for (size_t i = 0; i < text.size(); ++i) {
        if (IsHighSurrogate(text[i]) && i + 1 < text.size() && IsLowSurrogate(text[i + 1]))
            ++i;
        ++characters;
    }
    if (characters <= 55)
        return text;
    size_t start = text.size();
    for (size_t i = 0; i < 52; ++i) {
        --start;
        if (IsLowSurrogate(text[start]) && start && IsHighSurrogate(text[start - 1]))
            --start;
    }
    return L"..." + text.substr(start);
}

}

UpdaterUI::~UpdaterUI() {
    if (window_)
        DestroyWindow(window_);
    ReleaseFonts();
}

// 窗口布局使用同一 DPI 比例，保持命中测试和绘制尺寸一致。
int UpdaterUI::Scale(int value) const {
    return MulDiv(value, static_cast<int>(dpi_), 96);
}

// 字体未选入绘图上下文时统一释放，随后重建或析构可安全重复调用。
void UpdaterUI::ReleaseFonts() {
    for (HFONT font : {titleFont_, stepFont_, detailFont_}) {
        if (font)
            DeleteObject(font);
    }
    titleFont_ = stepFont_ = detailFont_ = nullptr;
}

// DPI 变化时在窗口线程替换字体；创建失败的空句柄由渲染器使用默认字体代替。
void UpdaterUI::CreateFonts(UINT dpi) {
    ReleaseFonts();
    dpi_ = dpi ? dpi : 96;
    titleFont_ = CreateUpdaterFont(dpi_, 15, FW_SEMIBOLD);
    stepFont_ = CreateUpdaterFont(dpi_, 10, FW_NORMAL);
    detailFont_ = CreateUpdaterFont(dpi_, 9, FW_NORMAL);
}

bool UpdaterUI::Create() {
    title_ = Tr(L"正在安装更新");
    step_ = Tr(L"准备中…");
    const HINSTANCE instance = GetModuleHandleW(nullptr);
    const HDC screen = GetDC(nullptr);
    const int dpi = screen ? GetDeviceCaps(screen, LOGPIXELSY) : 96;
    if (screen)
        ReleaseDC(nullptr, screen);
    CreateFonts(dpi > 0 ? static_cast<UINT>(dpi) : 96);

    WNDCLASSEXW windowClass{};
    windowClass.cbSize = sizeof(windowClass);
    windowClass.style = CS_DROPSHADOW;
    windowClass.lpfnWndProc = WindowProc;
    windowClass.hInstance = instance;
    windowClass.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    windowClass.hIcon = LoadIconW(instance, MAKEINTRESOURCEW(IDI_APP_ICON));
    windowClass.hIconSm = windowClass.hIcon;
    windowClass.lpszClassName = kWindowClass;
    if (!RegisterClassExW(&windowClass) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS)
        return false;

    RECT rectangle{0, 0, Scale(kUpdaterClientWidth), Scale(kUpdaterClientHeight)};
    if (!AdjustWindowRectEx(&rectangle, kWindowStyle, FALSE, WS_EX_TOPMOST))
        return false;
    const int width = rectangle.right - rectangle.left;
    const int height = rectangle.bottom - rectangle.top;
    RECT workArea{};
    if (!SystemParametersInfoW(SPI_GETWORKAREA, 0, &workArea, 0)) {
        workArea.right = GetSystemMetrics(SM_CXSCREEN);
        workArea.bottom = GetSystemMetrics(SM_CYSCREEN);
    }
    const int x = workArea.left + (workArea.right - workArea.left - width) / 2;
    const int y = workArea.top + (workArea.bottom - workArea.top - height) / 2;
    const auto caption = Tr(L"d3dxSkinManage-Plus 更新程序");
    const HWND window = CreateWindowExW(WS_EX_TOPMOST, kWindowClass, caption.c_str(), kWindowStyle, x, y, width, height,
                                        nullptr, nullptr, instance, this);
    if (!window)
        return false;
    UpdateWindowShape(window, dpi_);
    const BOOL dark = TRUE;
    if (FAILED(DwmSetWindowAttribute(window, 20, &dark, sizeof(dark)))) {
        DwmSetWindowAttribute(window, 19, &dark, sizeof(dark));
    }
    const HMENU menu = GetSystemMenu(window, FALSE);
    if (menu)
        EnableMenuItem(menu, SC_CLOSE, MF_BYCOMMAND | MF_GRAYED);
    ShowWindow(window, SW_SHOWNORMAL);
    UpdateWindow(window);
    return true;
}

// 锁内确认窗口仍可接收消息，投递成功才转移堆对象所有权；直接应用前释放锁。
void UpdaterUI::Post(Update update) {
    {
        const std::lock_guard<std::mutex> lock(windowMutex_);
        if (!window_)
            return;
        if (GetWindowThreadProcessId(window_, nullptr) != GetCurrentThreadId()) {
            auto message = std::make_unique<Update>(std::move(update));
            if (PostMessageW(window_, kUpdateMessage, 0, reinterpret_cast<LPARAM>(message.get()))) {
                message.release(); // 消息投递成功后由窗口过程负责释放。
            }
            return;
        }
    }
    // 同步执行时已处于窗口线程，立即应用结果，避免消息队列积压。

    // 先释放互斥锁，Apply 在定时器创建失败时可能销毁窗口。
    Apply(update);
}

void UpdaterUI::SetStep(int step) {
    Post({Action::Step, step, 0, {}});
}
void UpdaterUI::SetDetail(const std::wstring& text) {
    Post({Action::Detail, 0, 0, ShortDetail(text)});
}
void UpdaterUI::SetProgress(double percentage) {
    Post({Action::Progress, 0, percentage, {}});
}
void UpdaterUI::SetIndeterminate() {
    Post({Action::Indeterminate, 0, 0, {}});
}
void UpdaterUI::StopIndeterminate() {
    Post({Action::Stop, 0, 0, {}});
}
void UpdaterUI::ShowResult(bool success) {
    Post({Action::Result, success ? 1 : 0, 0, {}});
}
void UpdaterUI::CloseAfter(int milliseconds) {
    Post({Action::CloseAfter, milliseconds, 0, {}});
}

// 状态消息统一在窗口线程生效，定时器创建失败可能在此触发窗口销毁。
void UpdaterUI::Apply(const Update& update) {
    switch (update.action) {
    case Action::Step: {
        currentStep_ = update.number;
        constexpr const wchar_t* names[] = {N_(L"等待主程序退出"), N_(L"同步更新文件"), N_(L"启动新版本"),
                                            N_(L"清理临时文件")};
        const auto name = update.number >= 1 && update.number <= 4 ? names[update.number - 1] : L"";
        step_ = Tr(L"步骤 {n}/{total}: {name}…",
                   {{L"n", std::to_wstring(update.number)}, {L"total", L"4"}, {L"name", Tr(name)}});
        break;
    }
    case Action::Detail:
        detail_ = update.text;
        break;
    case Action::Progress:
        indeterminate_ = false;
        KillTimer(window_, kAnimationTimer);
        percentage_ = std::isfinite(update.value) ? std::clamp(update.value, 0.0, 100.0) : 0;
        showPercentage_ = true;
        break;
    case Action::Indeterminate:
        indeterminate_ = true;
        showPercentage_ = false;
        animationPhase_ = 0;
        SetTimer(window_, kAnimationTimer, 15, nullptr);
        break;
    case Action::Stop:
        indeterminate_ = false;
        KillTimer(window_, kAnimationTimer);
        percentage_ = 0;
        break;
    case Action::Result:
        result_ = update.number ? 1 : 0;
        indeterminate_ = false;
        KillTimer(window_, kAnimationTimer);
        detail_.clear();
        if (update.number) {
            title_ = Tr(L"更新完成");
            step_ = Tr(L"更新已完成，此窗口即将关闭。");
            percentage_ = 100;
            showPercentage_ = true;
        } else {
            title_ = Tr(L"更新遇到问题");
            step_ = Tr(L"更新出错，请查看 logs/ 下的日志");
            showPercentage_ = false;
        }
        break;
    case Action::CloseAfter:
        if (!SetTimer(window_, kCloseTimer, static_cast<UINT>(std::max(1, update.number)), nullptr)) {
            DestroyWindow(window_);
        }
        break;
    }
    if (window_)
        InvalidateRect(window_, nullptr, FALSE);
}

// 组装当前帧的文案与状态；LoadIconW 返回共享资源图标，此处不销毁它。
void UpdaterUI::PaintContents(HDC dc) {
    UpdaterVisualState state;
    state.caption = Tr(L"d3dxSkinManage-Plus 更新程序");
    state.appIcon = LoadIconW(GetModuleHandleW(nullptr), MAKEINTRESOURCEW(IDI_APP_ICON));
    state.title = title_;
    state.step = step_;
    state.detail = detail_;
    state.footer = Tr(L"此窗口将在更新结束后自动关闭。");
    state.steps = {Tr(L"等待退出"), Tr(L"替换文件"), Tr(L"启动新版"), Tr(L"清理文件")};
    state.currentStep = currentStep_;
    state.result = result_;
    state.percentage = percentage_;
    state.showPercentage = showPercentage_;
    state.indeterminate = indeterminate_;
    state.animationPhase = animationPhase_;
    DrawUpdater(dc, dpi_, state, titleFont_, stepFont_, detailFont_);
}

// 临时位图在复制到目标后解除选入并释放；分配失败时直接绘制到目标上下文。
void UpdaterUI::Paint(HDC dc) {
    RECT client{};
    GetClientRect(window_, &client);
    const HDC memory = CreateCompatibleDC(dc);
    const HBITMAP bitmap = CreateCompatibleBitmap(dc, std::max(1L, client.right), std::max(1L, client.bottom));
    if (memory && bitmap) {
        const auto previous = SelectObject(memory, bitmap);
        PaintContents(memory);
        BitBlt(dc, 0, 0, client.right, client.bottom, memory, 0, 0, SRCCOPY);
        SelectObject(memory, previous);
    } else {
        PaintContents(dc);
    }
    if (bitmap)
        DeleteObject(bitmap);
    if (memory)
        DeleteDC(memory);
}

// Windows 回调只借用实例，实例必须活到 WM_NCDESTROY 完成。
LRESULT CALLBACK UpdaterUI::WindowProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    auto self = reinterpret_cast<UpdaterUI*>(GetWindowLongPtrW(window, GWLP_USERDATA));
    if (message == WM_NCCREATE) {
        self = static_cast<UpdaterUI*>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams);
        SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
        const std::lock_guard<std::mutex> lock(self->windowMutex_);
        self->window_ = window;
    }
    return self ? self->HandleMessage(window, message, wParam, lParam)
                : DefWindowProcW(window, message, wParam, lParam);
}

// 消费状态消息后释放其内存；销毁时先禁止新投递，再清空残留消息并结束消息循环。
LRESULT UpdaterUI::HandleMessage(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    switch (message) {
    case kUpdateMessage: {
        const std::unique_ptr<Update> update(reinterpret_cast<Update*>(lParam));
        if (update)
            Apply(*update);
        return 0;
    }
    case WM_ERASEBKGND:
        return 1;
    case WM_NCHITTEST: {
        const LRESULT original = DefWindowProcW(window, message, wParam, lParam);
        if (original != HTCLIENT)
            return original;
        POINT point{GET_X_LPARAM(lParam), GET_Y_LPARAM(lParam)};
        ScreenToClient(window, &point);
        return HitTestUpdaterCaption(point, dpi_);
    }
    case WM_CLOSE:
        return 0;
    case WM_SYSCOMMAND:
        if ((wParam & 0xfff0) == SC_CLOSE || (wParam & 0xfff0) == SC_MINIMIZE || (wParam & 0xfff0) == SC_MAXIMIZE)
            return 0;
        break;
    case WM_PAINT: {
        PAINTSTRUCT paint{};
        const HDC dc = BeginPaint(window, &paint);
        Paint(dc);
        EndPaint(window, &paint);
        return 0;
    }
    case WM_TIMER:
        if (wParam == kAnimationTimer) {
            const int cycle = std::max(2, 2 * Scale(568 - 60));
            animationPhase_ = (animationPhase_ + std::max(1, Scale(2))) % cycle;
            InvalidateRect(window, nullptr, FALSE);
        } else if (wParam == kCloseTimer) {
            DestroyWindow(window);
        }
        return 0;
    case WM_DPICHANGED: {
        CreateFonts(HIWORD(wParam));
        const auto suggested = reinterpret_cast<const RECT*>(lParam);
        SetWindowPos(window, HWND_TOPMOST, suggested->left, suggested->top, suggested->right - suggested->left,
                     suggested->bottom - suggested->top, SWP_NOACTIVATE);
        UpdateWindowShape(window, dpi_);
        InvalidateRect(window, nullptr, FALSE);
        return 0;
    }
    case WM_DESTROY:
        KillTimer(window, kAnimationTimer);
        KillTimer(window, kCloseTimer);
        return 0;
    case WM_NCDESTROY: {
        {
            const std::lock_guard<std::mutex> lock(windowMutex_);
            window_ = nullptr;
        }
        MSG pending{};
        int exitCode = 0;
        while (PeekMessageW(&pending, window, kUpdateMessage, kUpdateMessage, PM_REMOVE)) {
            // WM_QUIT 不受 PeekMessage 范围过滤，须保留该退出消息。

            if (pending.message == WM_QUIT)
                exitCode = static_cast<int>(pending.wParam);
            else
                delete reinterpret_cast<Update*>(pending.lParam);
        }
        SetWindowLongPtrW(window, GWLP_USERDATA, 0);
        PostQuitMessage(exitCode);
        break;
    }
    }
    return DefWindowProcW(window, message, wParam, lParam);
}

void UpdaterUI::Run() {
    MSG message{};
    for (;;) {
        const BOOL result = GetMessageW(&message, nullptr, 0, 0);
        if (!result)
            break;
        if (result == -1) {
            LogWarning(L"窗口消息循环失败: " + Win32Error(GetLastError()));
            if (window_)
                DestroyWindow(window_);
            break;
        }
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
}

}
