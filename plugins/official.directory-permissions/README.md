# 目录权限修复

用于修复旧版 Plus 由 Python 私有临时目录权限引起的用户访问问题，尤其是发布后的预览缓存。
这是独立分发的可选插件，不随主程序默认安装。新版程序已在创建临时目录时恢复父目录继承，
不需要在每次启动或更新后执行历史修复。

## 使用方法

1. 在 Plus 的插件页使用 **安装 ZIP** 安装此插件，并确认文件读写及原生接口权限、启用插件。
2. 在插件详情的 **操作** 区打开 **目录权限修复**，确认显示的应用数据目录，点击 **开始修复**。
3. 等待窗口显示检查、修复和失败数量。有失败时查看具体路径，排除占用或访问问题后可手动重试。
4. 修复成功后可停用或卸载插件。已恢复的权限不依赖插件，不会因卸载失效。

启用插件、打开窗口和启动应用均不运行扫描。修复通过宿主后台任务执行，不阻塞界面。
窗口提供 **关闭** 和 **开始修复** 两个操作，关闭按钮始终可用。扫描过程中关闭窗口会停止后续工作，
已经完成的修复保留，之后可重新打开窗口运行修复；无需单独的取消按钮，也不会人为延长扫描时间。
默认弹窗为 760×400，按屏幕工作区适配并居中显示，也可手动调整大小。
如果检查结果为零个修复，说明未发现符合旧临时目录特征的权限；插件不会强行覆盖其他访问限制。

## 范围与权限

仅支持 Windows，要求宿主支持插件 SDK 2.1。插件由所在的标准 `plugins/<id>/` 目录定位应用数据根，
只检查该根下的 `config`、`data`、`cache`、`logs`、`plugins` 和 `Updater` 及其子目录。
不扫描整块磁盘或其他程序的系统临时目录，不跟随符号链接、目录联接或其他重解析点。

只为明确符合旧 Python 临时目录特征的受保护 ACL 启用父目录继承，保留已有允许项。
含自定义 SID、拒绝项或未知访问控制项的目录不会修改。不删除、移动或重新下载文件，也不授予
`Everyone` 完全控制；最终访问范围由原有父目录权限决定，父目录本身限制访问时仍保持该限制。

权限用途：`fs:read` 用于遍历目录和读取 ACL，`fs:write` 用于恢复继承，`native` 用于直接调用
Windows 安全描述符接口，`background` 用于可取消后台任务，`ui:views` 与 `ui:tools-page` 用于修复窗口及入口。
不需要网络、外部进程、资源库写入或持久化存储权限。原生权限检查随插件分发，
不导入宿主内部模块、不依赖维护脚本，也不增加一次性修复专用的宿主 SDK 接口。

## 开发者打包

在仓库根目录运行现有插件打包工具：

```powershell
.venv\Scripts\python.exe scripts/plugins/plugin_tool.py pack plugins/official.directory-permissions --output build/plugins/official.directory-permissions-1.0.2.zip
```

只分发生成的 ZIP，不包含测试目录、虚拟环境或个人数据。

## English

**Directory Permission Repair** is an optional, separately distributed plugin for existing Plus users affected by
private temporary-directory ACLs, including published preview caches. Windows and plugin SDK 2.1 are required.
New app versions restore parent-directory inheritance when creating temporary directories; routine startup repair
is unnecessary.

Install the ZIP from the Plugins page, approve its declared permissions, enable it, then open
**Directory Permission Repair** in the plugin details' **Action** section.
Review the displayed application data directory and click
**Start repair**. Results include checked, repaired and failed counts, with failed paths available for manual retry.
Disable or uninstall the plugin when finished; repaired permissions remain in effect.

App startup, plugin activation and opening the window never scan directories. Repair runs in a cancellable host-managed
background task. The dialog offers **Close** and **Start repair**. Close is always available; closing during a scan stops
remaining work and keeps completed repairs. There is no separate Cancel button or artificial scanning delay.
Only `config`, `data`, `cache`, `logs`,
`plugins` and `Updater` beneath the active application data root are checked. Links and reparse points are not followed.
Only recognized private temporary-directory ACLs receive normal parent inheritance; existing entries, custom restrictions
and all files are kept. No unrestricted `Everyone` access is granted. Existing restrictions in parent directories remain.

The plugin declares file read/write, native Windows API, background-task and tool-window permissions. It needs no network,
external process, mod-library write or persistent-storage permission, and uses no private host modules or maintenance scripts.

The default dialog size is 760×400, fitted to the available screen area and centered. The window remains resizable.
