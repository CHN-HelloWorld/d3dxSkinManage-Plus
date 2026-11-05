# 旧版数据迁移

`official.legacy-migration` 将 d3dxSkinManage 的本地资源复制到 d3dxSkinManage-Plus 当前选择的游戏。
需要插件 SDK 2.1。插件随安装包提供，在插件页启用后，通过工具箱的 **从旧版迁移** 打开向导。
激活插件和打开向导不会扫描文件或开始迁移。

## 使用

1. 在游戏页创建或选择目标游戏。向导会显示其名称，游戏身份始终用于检查迁移范围。
2. 选择旧版安装目录，即包含 `home` 和 `resources` 的目录，点击 **读取环境**。
3. 选择旧版用户环境，点击 **扫描预览**。检查可导入 Mod、分类、容量以及 **扫描与复制问题**。
4. 点击 **确认并复制**。所有项目通过宿主操作队列逐项导入，可以继续使用非模态向导并查看进度。
5. 查看成功、跳过、失败和取消数量。修正源数据或路径问题后，可重新扫描，或点击 **重试失败和取消项**。

## 迁移内容

- 读取旧版用户环境的本地索引和共享 `resources/mods/<SHA>` 资源，停用的索引不参与扫描。
- 保留分类与对象归属、名称、作者、说明、分级、标签、首个有效来源地址和可读取的本地预览图。
- 创建或复用同级同名同类型分类。复用已有节点时不覆盖其缩略图；没有归属的资源进入“未分类”。
- 缺失本体、无效索引、分类歧义及相同摘要的冲突元数据会显示在问题列表中。
  只有索引、远程地址或解压缓存的项目不会被当作完整 Mod 迁移，也不会自动下载。

容量是源资源的估算值，解包及保存预览可能需要更多空间。复制过程中保留旧版目录；新导入的 Mod 默认停用，
需要在 Plus 中检查并自行启用。部署目录、旧版启用状态、快捷键和程序设置不会迁移。

兼容范围依据旧项目提交 `b5f76a6844887e273978a9643c92046844d3b117` 的公开数据格式。
支持分类文本中的通配规则和缩略图相对路径重定向；缩略图仅精确匹配，无法复现的近似匹配会报告。
不读取第三方旧插件的自定义存储，路径中的符号链接或目录联接会报告为不支持。

## 停止、重试与重复内容

向导的 **停止** 按钮和宿主任务中心的停止操作都会终止后续项目，并等待当前项目到达安全边界。
当前项目已成功提交或已识别为重复内容时，仍保留对应的成功或跳过结果；其余项目可以重试。
关闭向导或停用插件也会停止后续操作。切换目标游戏会使旧预览失效，需要重新扫描。

插件在专属存储中记录“目标游戏 / 旧版 SHA / 新 Mod ID”，跳过前会确认该 Mod 仍存在且仍属于目标游戏。
记录不再有效时，宿主会重新校验源文件及内容。宿主识别为重复内容的 Mod 会保留现有数据，返回跳过结果，
不会用旧版元数据覆盖已整理的信息。旧版 SHA 用于校验来源文件，不直接作为 Plus 的 Mod ID 或内容指纹。

## 权限与资源管理

文件占用时宿主会在后台有限重试清理。仍无法清理的产物及 `data/recovery/import-cleanup/` 恢复记录会保留，
向导显示具体路径，重启和重建索引不会将其自动纳管。后续导入会重试；提交结果不明、身份变化或发现外部新增
文件时只保留并提示检查。成功条目也可能附带清理错误，请同时查看结果说明。

插件只使用 SDK 2.1、标准库和 PySide6。所需权限为：`mods:read`、`mods:write`、`fs:read`、`storage`、
`background`、`ui:views` 和 `ui:tools-page`。不需要网络、外部进程、删除资源或文件系统写入权限；
向 Plus 资源库的复制由宿主导入接口执行。窗口通过宿主样式接口跟随明暗主题，扫描由宿主后台任务运行，
业务提交和数据查询留在宿主线程。关闭后会清理任务订阅和定时器，并忽略已失效会话的迟到回调。

## English

**Legacy data migration** copies local d3dxSkinManage resources into the game currently selected in d3dxSkinManage-Plus.
It requires plugin SDK 2.1. Enable the bundled plugin on the Plugins page, then open **Migrate from the legacy app**
in the toolbox. Activating the plugin or opening its window does not scan or import files.

1. Create or select the target on the Games page. Check the target name in the migration window.
2. Choose the legacy installation folder containing `home` and `resources`, then select **Find environments**.
3. Select a user environment and **Scan preview**. Review mods, categories, estimated size, and all scan issues.
4. Select **Confirm and copy**. The host imports items sequentially; **Stop** requests a safe stop.
5. Review succeeded, skipped, failed, and cancelled items. Fix source problems and scan again, or retry failed and
   cancelled items from the existing preview.

The plugin preserves supported category and object assignments, names, authors, descriptions, ratings, tags,
the first valid source address, and readable local previews. Disabled indexes are excluded. Missing archives,
invalid indexes, ambiguous categories, and conflicting metadata remain visible as issues. Indexes, remote addresses,
and extracted caches alone are not imported or downloaded. Imported mods stay disabled. Deployment paths, enabled
states, shortcuts, and legacy app settings are excluded. Legacy files are preserved. Estimated source size may be
smaller than the space needed for extracted files and previews.

Compatibility follows the public data format at legacy commit `b5f76a6844887e273978a9643c92046844d3b117`.
Category wildcard rules and relative thumbnail redirection are supported. Thumbnail matching is exact; unsupported
approximate matches are reported. Custom storage from third-party legacy plugins, symbolic links, and junctions are
outside the supported input format.

Stopping from this window or the host task center ends the remaining queue at a safe boundary. An item already
committed or identified as a duplicate keeps its successful or skipped result. Completed imports survive a stop,
window closure, or plugin deactivation. Switching games invalidates the previous
preview. Migration history records the target game, source SHA, and actual target Mod ID; every recorded target is
checked before skipping. The host also detects duplicate content and preserves existing metadata. Legacy SHA values
validate source files and are never used as Plus Mod IDs or content fingerprints.

The plugin uses host-managed background tasks, serial SDK imports, and host theme bindings. It requests only
`mods:read`, `mods:write`, `fs:read`, `storage`, `background`, `ui:views`, and `ui:tools-page`. It does not use network
access or run legacy code. Window closure releases observers and timers and suppresses stale callbacks.

Locked output files receive limited cleanup retries. Remaining output is preserved with a receipt under
`data/recovery/import-cleanup/` and a path diagnostic; restarts and index rebuilds keep it out of ordinary Mod discovery.
Later imports retry cleanup. Uncertain commits, changed identities, and externally added files are preserved for
inspection. A successfully imported item can still carry a cleanup error, so also review its result details.
