#include "ui_render.h"

#include <algorithm>
#include <cmath>

namespace updater {
namespace {

constexpr COLORREF kBackground = RGB(31, 32, 34);
constexpr COLORREF kSurface = RGB(39, 41, 44);
constexpr COLORREF kBorder = RGB(57, 60, 65);
constexpr COLORREF kText = RGB(243, 244, 246);
constexpr COLORREF kSecondary = RGB(185, 191, 200);
constexpr COLORREF kMuted = RGB(143, 151, 163);
constexpr COLORREF kAccent = RGB(112, 191, 239);
constexpr COLORREF kSuccess = RGB(135, 206, 161);
constexpr COLORREF kError = RGB(255, 153, 164);

// 使用临时画刷和画笔绘制圆角矩形，恢复原选入对象后释放临时资源。
void Round(HDC dc, RECT rect, int radius, COLORREF fill, COLORREF border) {
    const HBRUSH brush = CreateSolidBrush(fill);
    const HPEN pen = CreatePen(PS_SOLID, 1, border);
    const auto oldBrush = SelectObject(dc, brush);
    const auto oldPen = SelectObject(dc, pen);
    RoundRect(dc, rect.left, rect.top, rect.right, rect.bottom, radius, radius);
    SelectObject(dc, oldBrush);
    SelectObject(dc, oldPen);
    DeleteObject(brush);
    DeleteObject(pen);
}

// 绘制单行省略文本，不解释助记符；借用字体并恢复原字体，文本颜色由外层 SaveDC 负责恢复。
void Text(HDC dc, const std::wstring& text, RECT rect, HFONT font, COLORREF color, UINT align = DT_LEFT) {
    const auto previous = SelectObject(dc, font ? font : GetStockObject(DEFAULT_GUI_FONT));
    SetTextColor(dc, color);
    DrawTextW(dc, text.c_str(), static_cast<int>(text.size()), &rect,
              DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX | DT_END_ELLIPSIS | align);
    SelectObject(dc, previous);
}

// 以临时画笔绘制像素坐标线段；恢复原画笔，当前位置变化由外层 SaveDC 负责恢复。
void Line(HDC dc, int x1, int y1, int x2, int y2, int width, COLORREF color) {
    const HPEN pen = CreatePen(PS_SOLID, width, color);
    const auto previous = SelectObject(dc, pen);
    MoveToEx(dc, x1, y1, nullptr);
    LineTo(dc, x2, y2);
    SelectObject(dc, previous);
    DeleteObject(pen);
}

}

HFONT CreateUpdaterFont(UINT dpi, int points, int weight) {
    return CreateFontW(-MulDiv(points, static_cast<int>(dpi), 72), 0, 0, 0, weight, FALSE, FALSE, FALSE,
                       DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
                       DEFAULT_PITCH | FF_DONTCARE, L"Microsoft YaHei UI");
}

LRESULT HitTestUpdaterCaption(POINT point, UINT dpi) {
    const int height = MulDiv(kUpdaterCaptionHeight, static_cast<int>(dpi), 96);
    const int right = MulDiv(kUpdaterClientWidth - 42, static_cast<int>(dpi), 96);
    return point.x >= 0 && point.x < right && point.y >= 0 && point.y < height ? HTCAPTION : HTCLIENT;
}

void DrawUpdater(HDC dc, UINT dpi, const UpdaterVisualState& state, HFONT titleFont, HFONT stepFont, HFONT detailFont) {
    // 局部绘图适配器将所有逻辑坐标统一缩放，并保证线宽至少为一个像素。
    const auto scale = [dpi](int value) { return MulDiv(value, static_cast<int>(dpi), 96); };
    const auto rect = [&scale](int x, int y, int right, int bottom) {
        return RECT{scale(x), scale(y), scale(right), scale(bottom)};
    };
    const auto line = [&](int x1, int y1, int x2, int y2, COLORREF color, int width = 1) {
        Line(dc, scale(x1), scale(y1), scale(x2), scale(y2), std::max(1, scale(width)), color);
    };
    const int saved = SaveDC(dc);
    const HBRUSH caption = CreateSolidBrush(RGB(37, 39, 42));
    const RECT captionRect = rect(0, 0, kUpdaterClientWidth, kUpdaterCaptionHeight);
    FillRect(dc, &captionRect, caption);
    DeleteObject(caption);
    SetBkMode(dc, TRANSPARENT);
    if (state.appIcon)
        DrawIconEx(dc, scale(12), scale(7), state.appIcon, scale(16), scale(16), 0, nullptr, DI_NORMAL);
    Text(dc, state.caption, rect(state.appIcon ? 36 : 12, 2, 566, 29), detailFont, kSecondary);
    line(595, 11, 603, 19, kMuted);
    line(603, 11, 595, 19, kMuted);
    SetViewportOrgEx(dc, 0, scale(kUpdaterCaptionHeight), nullptr);
    const HBRUSH background = CreateSolidBrush(kBackground);
    const RECT client = rect(0, 0, kUpdaterClientWidth, kUpdaterBodyHeight);
    FillRect(dc, &client, background);
    DeleteObject(background);
    SetBkMode(dc, TRANSPARENT);
    const COLORREF status = state.result == 1 ? kSuccess : state.result == 0 ? kError : kAccent;
    Round(dc, rect(26, 24, 74, 72), scale(18), kSurface, kBorder);
    if (state.result == 1) {
        line(38, 48, 47, 57, status, 2);
        line(47, 57, 64, 39, status, 2);
    } else if (state.result == 0) {
        line(50, 35, 65, 60, status, 2);
        line(65, 60, 35, 60, status, 2);
        line(35, 60, 50, 35, status, 2);
        line(50, 44, 50, 52, status, 2);
        line(50, 55, 50, 57, status, 2);
    } else {
        line(38, 40, 60, 40, status, 2);
        line(60, 40, 55, 35, status, 2);
        line(60, 40, 55, 45, status, 2);
        line(62, 56, 40, 56, status, 2);
        line(40, 56, 45, 51, status, 2);
        line(40, 56, 45, 61, status, 2);
    }
    Text(dc, state.title, rect(92, 22, 594, 53), titleFont, kText);
    Text(dc, L"d3dxSkinManage-Plus", rect(92, 55, 594, 76), detailFont, kMuted);
    line(26, 96, 594, 96, kBorder);

    for (int index = 0; index < 4; ++index) {
        const int center = 90 + index * 146;
        if (index < 3)
            line(center + 20, 128, center + 126, 128, kBorder);
        const bool active = state.result < 0 && state.currentStep == index + 1;
        const COLORREF marker = state.result == 1 ? kSuccess : active ? kAccent : kMuted;
        Round(dc, rect(center - 13, 115, center + 13, 141), scale(26), kSurface, marker);
        if (state.result == 1) {
            line(center - 6, 128, center - 1, 133, marker, 2);
            line(center - 1, 133, center + 7, 123, marker, 2);
        } else {
            Text(dc, std::to_wstring(index + 1), rect(center - 13, 115, center + 13, 141), detailFont, marker,
                 DT_CENTER);
        }
        Text(dc, state.steps[index], rect(center - 68, 149, center + 68, 173), detailFont,
             active || state.result == 1 ? kText : kMuted, DT_CENTER);
    }

    Round(dc, rect(26, 193, 594, 263), scale(14), kSurface, kBorder);
    Text(dc, state.step, rect(42, 201, 578, 229), stepFont, state.result == 0 ? kError : kText);
    Text(dc, state.detail, rect(42, 232, 578, 254), detailFont, kSecondary);

    RECT bar = rect(26, 282, 594, 288);
    Round(dc, bar, scale(6), kBorder, kBorder);
    const int width = bar.right - bar.left;
    if (state.indeterminate) {
        const int block = scale(60);
        const int travel = std::max(1, width - block);
        const int phase = state.animationPhase % (2 * travel);
        bar.left += phase <= travel ? phase : 2 * travel - phase;
        bar.right = bar.left + block;
    } else {
        const double value = std::isfinite(state.percentage) ? std::clamp(state.percentage, 0.0, 100.0) : 0;
        bar.right = bar.left + static_cast<LONG>(static_cast<double>(width) * value / 100.0);
    }
    if (bar.right > bar.left)
        Round(dc, bar, scale(6), status, status);
    Text(dc, state.footer, rect(26, 301, 514, 325), detailFont, kMuted);
    if (state.showPercentage) {
        Text(dc, std::to_wstring(static_cast<int>(state.percentage)) + L"%", rect(520, 301, 594, 325), detailFont,
             kSecondary, DT_RIGHT);
    }
    RestoreDC(dc, saved);
    const HPEN border = CreatePen(PS_SOLID, 1, kBorder);
    const auto oldPen = SelectObject(dc, border);
    const auto oldBrush = SelectObject(dc, GetStockObject(NULL_BRUSH));
    RoundRect(dc, 0, 0, scale(kUpdaterClientWidth), scale(kUpdaterClientHeight), scale(12), scale(12));
    SelectObject(dc, oldPen);
    SelectObject(dc, oldBrush);
    DeleteObject(border);
}

}
