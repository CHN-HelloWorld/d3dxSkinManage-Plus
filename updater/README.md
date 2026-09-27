# 原生更新器

使用 C++17 和 Windows SDK 实现的 x64 更新器。主程序负责检查版本、下载、解压和可选备份，
更新器在主程序退出后完成文件替换与重新启动。

## 构建

默认工程使用 Visual Studio 2022、MSVC v143 和 Windows 10/11 SDK。
安装“使用 C++ 的桌面开发”组件后，打开 [updater.sln](updater.sln)，选择 `Release | x64` 构建。

也可以由脚本自动定位已安装的工具集，并同时构建更新器、测试和离屏预览工具：

```powershell
python updater/tools/build.py
```

在开发者命令提示符中也可执行：

```bat
msbuild updater\updater.vcxproj /m /p:Configuration=Release /p:Platform=x64
```

安装了较新 Visual Studio 时，可在命令行选择已安装的工具集。例如 v145：

```bat
msbuild updater\updater.vcxproj /m /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v145
```

产物为 `updater/bin/x64/Release/updater.exe`。Release 构建默认复制到仓库的
`build/Updater/updater.exe`；传入 `/p:DeployUpdater=false` 可关闭复制，`UpdaterDeployDir` 可指定目标目录。
Debug 使用独立输出目录。Release 静态链接 CRT，程序不依赖 Qt、Python、.NET 或第三方 C++ 库。

## 与主程序的接口

```text
updater.exe <主程序PID> <解压目录> <安装目录> <保留列表JSON> <启动令牌>
```

主程序可执行文件为 `d3dxSkinManage-Plus.exe`。启动令牌使用与主程序一致的 HMAC-SHA256
参数，允许前后 120 秒的时间误差。直接双击更新器不会执行文件更新。

更新器通过进程句柄等待主程序退出，超过 30 秒或无法确认退出时停止操作。
启动新主程序使用 `ShellExecuteExW`，支持主程序的管理员权限清单。
参数校验或初始化失败返回 1；进入图形工作流程后的结果由窗口和日志报告。

日志保存到 `logs/updater/`，使用 UTF-8。主程序启动时清理超过 14 天的更新器日志。

