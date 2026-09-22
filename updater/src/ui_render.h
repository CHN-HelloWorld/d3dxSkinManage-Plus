#pragma once

#include <windows.h>
#include <array>
#include <string>

namespace updater {

// 以 96 DPI 为基准的逻辑尺寸，窗口与离屏预览共用同一布局。
inline constexpr int kUpdaterClientWidth = 620;
inline constexpr int kUpdaterCaptionHeight = 31;
inline constexpr int kUpdaterBodyHeight = 338;
inline constexpr int kUpdaterClientHeight = kUpdaterBodyHeight + kUpdaterCaptionHeight;

// 单帧绘制快照；持有文案副本，借用图标，绘制期间由调用方保持内容与资源有效。
struct UpdaterVisualState {
    std::wstring caption;
    HICON appIcon = nullptr;
    std::wstring title;
    std::wstring step;
    std::wstring detail;
    std::wstring footer;
    std::array<std::wstring, 4> steps;
    int currentStep = 0;
    int result = -1; // -1 表示进行中，0 表示失败，1 表示完成。
    // 显示百分比时须为可转为整数的有限值；正常界面入口将其限制在 0 至 100。
    double percentage = 0;
    bool showPercentage = false;
    bool indeterminate = false;
    int animationPhase = 0; // 非负像素相位，用于不定进度条的往返动画。
};

// 按点数与 DPI 创建字体，失败返回空句柄；成功返回的 HFONT 由调用方用 DeleteObject 释放。
HFONT CreateUpdaterFont(UINT dpi, int points, int weight);
// 输入为客户区像素坐标，返回可拖动标题区或普通客户区；右侧关闭图案不属于拖动区域。
LRESULT HitTestUpdaterCaption(POINT point, UINT dpi);
// 向借用的 HDC 绘制完整一帧；DPI 应有效且非零，字体为空时使用系统默认字体。
// 不接管图标、字体或 HDC，临时 GDI 对象在绘制后释放；调用方负责串行使用绘图上下文。
void DrawUpdater(HDC dc, UINT dpi, const UpdaterVisualState& state, HFONT titleFont, HFONT stepFont, HFONT detailFont);

}
