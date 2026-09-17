# 测试说明

## 自动化测试

在仓库根安装开发依赖后执行：

```powershell
.venv\Scripts\python.exe -m pytest
```

`core/` 验证数据模型、文件身份、导入部署和恢复；`services/` 验证文件格式、网络和更新；
`plugins/` 验证 SDK 与运行时；`ui/` 验证 PySide6 交互和资源生命周期；`perf/` 验证基准工具的隔离与统计契约。

测试使用 pytest 临时目录和隔离的配置。不要把真实游戏目录或持久测试库作为 `--basetemp`。
测试夹具需恢复全局配置、单例和主题，并关闭窗口、工作线程和文件句柄。

GameBanana 数据夹具使用合成游戏、角色、作者、资源编号和文件内容摘要；保留外部接口字段及
归档、版本和编码差异，以验证实际协议行为。游戏名称匹配测试显式配置名称缓存或模拟在线候选。

无桌面环境可设置 `QT_QPA_PLATFORM=offscreen` 运行普通控件测试。
窗口系统、原生拖放、桌面捕获和渲染性能仍需在 Windows 桌面中验证；无桌面执行结果不能代表这些能力。
原生像素检查还要求测试线程位于当前可访问的输入桌面；无法访问或位于隔离桌面时会跳过，
这些跳过项不计为视觉验证通过。

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python.exe -m pytest -o faulthandler_timeout=60
```

C++ 更新器测试独立构建，见[更新器说明](../updater/README.md)。

## 性能验证

`scripts/performance/perf_mod_library.py` 创建隔离的合成库，支持按大小运行核心操作和真实 GUI 场景；
参数说明通过 `--help` 查看。`scripts/performance/benchmark_*.py` 覆盖卡片滚动、预览返回和图像内存生命周期，
`scripts/performance/perf_rebuild_responsiveness.py` 测量索引重建与界面心跳。

输出目录应使用独立临时路径。记录运行环境、库大小、轮次和实际样本，不把单次耗时或
无桌面运行结果当作桌面性能结论。

持久大库数据保存在仓库外的独立目录。已有环境通过
[`scripts/launch/open_experience.py`](../scripts/launch/open_experience.py) 打开，并显式指定数据根目录：

```powershell
.venv\Scripts\python.exe scripts/launch/open_experience.py --data-root "D:/TestLibraries/large-library"
```

该目录必须已存在；启动器使用当前仓库源码和指定目录中的配置、数据，退出后保留测试库。
仓库只保存启动脚本，完整数据集不会随 Git 分发。自动化测试通过临时目录验证启动器，不依赖本地大库。

## 桌面验证

- 使用独立配置添加游戏，确认切换游戏后的分类、预设与部署目标。
- 导入一个目录和一个压缩包，验证预览、启用、禁用及删除确认。
- 导入和导出分类预设，检查冲突提示和图片保存。
- 验证明暗主题、语言切换、快捷键、背景和弹窗缩放。
- 验证在线浏览、下载取消、更新检查和安装目标确认。
- 启用、停用和卸载随包插件，确认回调和界面资源释放。
- 用隔离发行目录验证更新成功、文件占用失败与恢复，不操作日常使用的数据。
