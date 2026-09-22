#pragma once

#include <windows.h>
#include <mutex>
#include <string>

namespace updater {

// 拥有原生窗口和字体；Create、Run 与析构须在同一窗口线程执行。
// 状态提交方法可由工作线程调用，绘制状态只在窗口线程修改；析构前须停止所有外部调用。
class UpdaterUI {
  public:
    // 建立无窗口、无字体的初始状态。
    UpdaterUI() = default;
    // 销毁仍存在的窗口并释放字体，窗口销毁过程会回收尚未处理的状态消息。
    ~UpdaterUI();
    // 禁止复制窗口及其资源所有权。
    UpdaterUI(const UpdaterUI&) = delete;
    // 禁止复制赋值，防止窗口用户数据指向错误对象。
    UpdaterUI& operator=(const UpdaterUI&) = delete;

    // 在当前线程创建并显示置顶窗口；窗口类注册或窗口创建等必要步骤失败时返回 false。
    bool Create();
    // 运行创建线程的消息循环直至 WM_QUIT；取消息失败时记录警告并销毁窗口。
    void Run();
    // 提交步骤编号，1 至 4 对应流程阶段；其他值仍显示编号，但不匹配步骤名称。
    void SetStep(int step);
    // 复制并提交详情，超过 55 个字符时保留末尾 52 个字符并加省略号，不拆开 UTF-16 代理对。
    void SetDetail(const std::wstring& text);
    // 提交确定进度并停止动画；应用时限制为 0 至 100，非有限数值按 0 处理。
    void SetProgress(double percentage);
    // 提交不定进度状态，在窗口线程启动动画定时器并隐藏百分比。
    void SetIndeterminate();
    // 请求停止动画并清零进度，不主动改变百分比的显示开关。
    void StopIndeterminate();
    // 提交成功或失败状态，停止动画并清空详情；此方法不安排关窗。
    void ShowResult(bool success);
    // 请求至少 1 毫秒后关窗；关闭定时器创建失败时立即销毁窗口。
    void CloseAfter(int milliseconds);

  private:
    // 跨线程消息中的操作类别，决定 Update 各字段的含义。
    enum class Action { Step, Detail, Progress, Indeterminate, Stop, Result, CloseAfter };
    // 自包含的状态消息；成功入队后由窗口过程接管分配并释放。
    struct Update {
        Action action;
        int number = 0;
        double value = 0;
        std::wstring text;
    };

    // 同线程直接应用，跨线程投递；窗口不存在或投递失败时丢弃并回收消息。
    void Post(Update update);
    // 仅由窗口线程更新绘制状态、定时器或窗口生命周期。
    void Apply(const Update& update);
    // 向借用的绘图上下文绘制一帧，优先使用双缓冲。
    void Paint(HDC dc);
    // 从窗口状态构造渲染快照，字体和图标仅供本次绘制借用。
    void PaintContents(HDC dc);
    // 释放旧字体并按新 DPI 创建资源，DPI 为 0 时使用 96。
    void CreateFonts(UINT dpi);
    // 删除所拥有的字体并清空句柄，可重复调用。
    void ReleaseFonts();
    // 将以 96 DPI 定义的逻辑尺寸换算为当前像素尺寸。
    int Scale(int value) const;
    // 处理状态消息、绘制、定时器及销毁；未处理消息交给 DefWindowProcW。
    LRESULT HandleMessage(HWND window, UINT message, WPARAM wParam, LPARAM lParam);
    // 在 WM_NCCREATE 绑定借用的对象指针，再把 Windows 消息转交给实例。
    static LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam);

    // 只保护窗口句柄与消息投递的交接，不用于跨线程直接访问下方绘制状态。
    std::mutex windowMutex_;
    HWND window_ = nullptr;
    UINT dpi_ = 96;
    HFONT titleFont_ = nullptr;
    HFONT stepFont_ = nullptr;
    HFONT detailFont_ = nullptr;
    std::wstring title_;
    std::wstring step_;
    std::wstring detail_;
    double percentage_ = 0;
    bool showPercentage_ = false;
    bool indeterminate_ = false;
    int animationPhase_ = 0;
    int currentStep_ = 0;
    int result_ = -1;
};

}
