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

只构建用于发行的更新器时执行：

```powershell
python updater/tools/build.py --updater-only
```

根目录构建菜单 `[5]` 使用这一入口。菜单 `[4]` 及云端发行工作流会在同步版本后自动执行，
无需单独预编译更新器。脚本构建关闭部署复制，由主程序发行步骤将更新器加入程序包。

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

## 文件更新与恢复

同步前检查完整计划、路径关系和重解析点。文件先复制到暂存位置，再替换目标；
占用文件可通过改名后替换的方式处理。复制失败时停止清理和启动，保留恢复材料并尝试恢复备份。

`data/`、`config/`、`cache/`、`logs/`、`plugins/` 和 `Updater/` 始终受保护。
命令行和更新清单可以追加保护范围。缺失的语言包可以补充，已有语言文件不会被覆盖。

清单由主程序构建脚本生成，必须包含 `[managed]`，可包含 `[preserve]`。
删除旧发行文件时优先使用 `Updater/installed.manifest`，首次更新可使用安装根目录的
`files.manifest`。新版清单或已安装清单不可读时不删除旧文件。

成功同步后先保存新清单，再启动主程序并清理解压缓存和备份。
启动失败时保留恢复材料；程序不提供启动后的健康检查和自动回滚。

## 更新器自身替换

更新包可包含 `Updater/updater.exe`。更新器先验证其 x64 PE 结构并写入同目录暂存文件，
再将当前文件改名为 `updater.exe.old_update.<pid>.<utc-ms>.<serial>`，发布新版文件。
旧进程继续使用已经映射的映像。

主程序同步失败时独立恢复旧更新器；同步成功后提交更新器替换。
主程序后续启动会重试清理已退出的旧映像。仅清理直属目录中符合当前命名规则且内容有效的
可执行文件，不递归进入缓存，不跟随链接，并保留唯一可用的恢复副本。
更新包没有附带更新器时继续使用现有版本。

## 验证

```bat
msbuild updater\tests\updater_tests.vcxproj /m /p:Configuration=Release /p:Platform=x64
updater\tests\bin\x64\Release\updater_tests.exe
```

新工具集同样可通过 `/p:PlatformToolset=v145` 指定。测试可加 `--filter manifest`、
`--filter sync`、`--filter process` 或 `--filter self_update` 筛选。
测试只使用独立临时目录，进程用例运行测试程序自身的副本，不启动真实主程序。

源码格式使用根目录 `.clang-format` 和 clang-format 18.1.8。
主程序发行包的组合构建见[构建指南](../docs/构建指南.md)。
