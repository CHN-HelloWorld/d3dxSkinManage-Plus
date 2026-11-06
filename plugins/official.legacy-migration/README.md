# d3dxSkinManage数据迁移

`official.legacy-migration` 将 d3dxSkinManage 的本地资源复制到 d3dxSkinManage-Plus 当前选择的游戏。
当前插件版本为 0.3.5，使用 SDK 2.1。插件随安装包提供，在插件页启用后，通过工具箱的 **d3dxSkinManage数据迁移** 打开向导。
激活插件和打开向导不会扫描文件或开始迁移。

## 使用

1. 在游戏页创建或选择要导入的游戏，打开向导，核对 **导入到** 显示的游戏名称。
2. **选择文件夹**：选择旧版程序所在的文件夹（包含 `home` 和 `resources`），向导会自动识别数据。找到多份数据时，选择本次要迁移的旧版游戏数据，再点击 **检查迁移内容**。也可粘贴路径后直接点击该按钮。
3. **确认内容**：在同一页查看分类树中的 Mod、类型、状态、数量和大小。无法导入和需留意的条目同样显示在树中，选中后在 **条目详情** 查看扫描或复制阶段的诊断。仅在文件可能有修改或原始文件丢失时，选中 Mod 才会显示文件版本选项。
4. 点击 **开始迁移**，查看进度。旧版文件不变，新导入的 Mod 保持停用。需要中断时点击 **停止迁移**，已完成的内容会保留。
5. **迁移结果**：先查看已导入、已存在、未完成数量；按需展开 **查看迁移详情**。未完成项可 **重试未完成**；修复源数据后点击 **重新选择**，再 **检查迁移内容**，会重新扫描而不是复用旧列表。每个批次结束后仍自动打开 TXT 报告，也可点击 **打开报告**。

向导一次只显示一个步骤，底部操作和安全提醒保持可见。确认页参照分类预设的树形预览，仅保留分类树和条目详情两栏；宽窗口左右排列，窄窗口上下排列。搜索和选中条目只影响查看，不改变迁移范围。条目详情直接显示扫描与复制阶段的诊断，工具提示和报告保留同一诊断与完整路径。

## 迁移内容

窗口严格参照导入分类预设按步骤设置大小：默认打开的来源页和结果页使用 720×480，确认页及展开的结果详情使用 1160×780 逻辑像素，首次显示即设置实际宿主窗口并居中，不随 4K 屏幕放大。小屏幕按工作区减去 40 像素宽、60 像素高限制上界，空间不足时可滚动查看。确认页合并标题和目标游戏，文件大小与分类树标题同行，避免重复区域浪费高度。长列表和条目详情在各自区域滚动；文件版本选择位于详情顶部，不会被长说明挤到页面末尾。数字卡片分别显示可迁移 Mod、可导入角色／对象、普通分类和无法迁移项；对象与普通分类不重复计数，搜索不会改变这些数量。树中的类型和状态分别占用独立列，左右内容区底部对齐。无法导入对象和需留意条目也显示在树中，状态分别为“无法导入”和“需留意”。可准确关联的提示跟随原条目；分类存在歧义时放在“未确定归属的条目”下，不猜测归属、不增加迁移范围。

- 读取旧版用户环境的启用及停用索引和共享 `resources/mods/<索引键>` 原包；索引键支持 SHA 和可读文件名。停用索引只有在本地原包存在时才纳入迁移。
- 相同索引键同时出现在启用与停用索引中时，优先保留启用索引的元数据并显示提示；同一启停状态的冲突元数据及多分类歧义仍拒绝迁移。
- 保留分类与对象归属、名称、作者、说明、分级、标签、首个有效来源地址和可读取的本地预览图。
- 创建或复用同级同名同类型分类。复用已有节点时不覆盖其缩略图；没有归属的资源进入“未分类”。
- 缺失本体、无效索引、分类歧义及相同摘要的冲突元数据会显示在目录树中，选中后在条目详情查看具体诊断。只有索引或远程地址的项目不会导入，也不会自动下载。

容量是源资源的估算值，解包及保存预览可能需要更多空间。复制过程中保留旧版目录；新导入的 Mod 默认停用，
需要在 Plus 中检查并自行启用。部署目录、旧版启用状态、加载器快捷键和旧版程序设置不会迁移。
没有独立的快捷键或加载器配置迁移；选择解压缓存时，缓存内 Mod 文件（包括已修改的 INI）会随资源一同复制。

兼容范围依据旧项目提交 `b5f76a6844887e273978a9643c92046844d3b117` 的公开数据格式。
支持分类文本中的通配规则和缩略图相对路径重定向。缩略图优先精确匹配；按旧版配置支持
`key-in only`、`similarity only`、`similarity threshold` 和 `similarity/key-in` 四种近似模式，使用近似结果时提示检查；未知模式仅精确匹配并显示警告。
读取共享 `resources/preview`、全屏 `resources/preview_screen` 中按 SHA 命名的图片及 SHA 子目录，
共享 SHA 目录作为专用预览目录读取所有支持的图片。启用／停用缓存中递归读取名称以 `preview` 开头的图片，
并读取显式 `preview`、`preview_screen` 子目录内的所有支持图片（包括嵌套目录）。
支持 PNG、JPG、JPEG、BMP、GIF、WebP，按图片字节去重，不遍历任意纹理图片，也不覆盖 Plus 已有 Mod 的元数据或预览图。
不读取第三方旧插件的自定义存储，路径中的符号链接或目录联接会报告为不支持。

## 原包与缓存恢复

默认优先使用原包，导入前由宿主校验原文件 SHA-1。扫描同时检查旧版环境的
`work/Mods/<索引键>` 和 `work/Mods/disabled-<索引键>` 解压缓存；多个缓存无法确定唯一来源时会忽略缓存并提示。
索引键不一定是摘要：对于可读文件名，扫描会计算原包的实际 SHA-1，报告分别记录索引键和原包 SHA-1；只有缓存时不伪造原包摘要。
路径、设备名、替代数据流和 Windows 文件名别名仍拒绝读取。
原包和有效缓存同时存在且可能有修改时，选择分类树中的 Mod，再用文件版本下拉框选择 **原始文件（推荐）** 或 **修改后的文件**。
缓存与原包不同，或无法可靠确认一致时，会显示恢复缓存修改的警告，是否恢复由用户决定。

启用索引只有有效缓存、没有原包时，可以恢复缓存，但会明确提示缓存可能不完整，复制后必须检查。
停用索引只有缓存或远程地址时仍不导入。缓存按目录相对路径及文件内容生成独立 SHA-256 指纹，
宿主在文件准备前后重新校验；来源变化会失败并要求重新扫描。该指纹不是原包 SHA-1，也不是 Plus 的 Mod ID。

## 停止、重试与重复内容

向导的 **停止** 按钮和宿主任务中心的停止操作都会终止后续项目，并等待当前项目到达安全边界。
当前项目已成功提交或已识别为重复内容时，仍保留对应的成功或跳过结果；其余项目可以重试。
关闭向导或停用插件也会停止后续操作。切换目标游戏会使旧预览失效，需要重新扫描。

插件在专属存储中记录“目标游戏 / 旧版 SHA / 新 Mod ID”，跳过前会确认该 Mod 仍存在且仍属于目标游戏。
记录不再有效时，宿主会重新校验源文件及内容。宿主识别为重复内容的 Mod 会保留现有数据，返回跳过结果，
不会用旧版元数据覆盖已整理的信息。原包记录使用旧版 SHA-1，缓存记录额外使用目录 SHA-256 指纹，
不会因原包已迁移而漏掉不同的缓存修改。旧版 SHA 用于校验原包，不直接作为 Plus 的 Mod ID 或内容指纹。

每次复制或重试批次结束时，宿主在系统临时目录创建唯一的 UTF-8 BOM `.txt` 文件，并交给系统默认应用打开。
复制期间关闭向导或切换游戏时，也会保存并打开中断报告；已提交结果保留，未开始项标为取消。
仍在处理、尚未取得宿主最终结果的项目会明确列为处理中，不会误记为成功或取消，请以宿主资源库及任务中心为准。
报告包含目标游戏身份、来源选择、原始 SHA、缓存指纹、目标 Mod ID、重复跳过原因、各项结果以及扫描／复制诊断。
完成后可点击 **打开报告** 再次生成并打开报告。默认应用无法打开时，文件仍保留，宿主通知显示完整路径；
报告生成失败不会撤销已完成的导入，仍可查看窗口中的结果。关闭向导或停用插件不会删除已生成的报告，系统可能按自己的临时文件策略清理；需要长期保留时另行保存副本。

## 权限与资源管理

文件占用时宿主会在后台有限重试清理。仍无法清理的产物及 `data/recovery/import-cleanup/` 恢复记录会保留，
向导显示具体路径，重启和重建索引不会将其自动纳管。后续导入会重试；提交结果不明、身份变化或发现外部新增
文件时只保留并提示检查。成功条目也可能附带清理错误，请同时查看结果说明。

插件只使用 SDK 2.1、标准库和 PySide6。所需权限为：`mods:read`、`mods:write`、`fs:read`、`storage`、
`background`、`ui:views` 和 `ui:tools-page`。不需要网络、外部进程、删除资源或文件系统写入权限；
向 Plus 资源库的复制由宿主导入接口执行；报告通过 `ctx.ui.show_text_report(text, title="") -> str` 交给宿主保存和打开，沿用 `ui:views` 权限，不增加权限、依赖或 API 版本。
窗口通过宿主样式接口跟随明暗主题，扫描由宿主后台任务运行，
业务提交和数据查询留在宿主线程。关闭后会清理任务订阅和定时器，并忽略已失效会话的迟到回调。

## English

**d3dxSkinManage Data Migration** copies local d3dxSkinManage resources into the game currently selected in d3dxSkinManage-Plus.
Plugin version 0.3.5 uses SDK 2.1. Enable the bundled plugin on the Plugins page, then open **d3dxSkinManage Data Migration**
in the toolbox. Activating the plugin or opening its window does not scan or import files.

1. Create or select the game on the Games page. Check **Import into** in the migration window.
2. **Choose folder**: select the legacy app folder containing `home` and `resources`. Data is detected automatically.
   If multiple sets are found, choose the legacy game data to migrate, then select **Review contents**.
   You can also paste a path and select **Review contents** directly.
3. **Review contents**: view the category tree with Mods, types, statuses, counts and sizes alongside item
   details. Excluded items and warnings stay in the tree; select them to read the recorded diagnostics. Selecting a Mod
   shows file-version options only when edits or recovery may be relevant.
4. Select **Start migration**. Legacy files stay unchanged and imported mods stay disabled.
   **Stop migration** stops subsequent work while retaining completed items.
5. **Results** shows imported, already present and unfinished counts. **Show migration details** expands the list.
   Use **Retry unfinished**, or fix source data and select **Start again**, then **Review contents** for a fresh scan.
   A TXT report still opens automatically after each batch; **Open report** opens it again.

Only the current step is shown. Review panels follow the category-preset design, with only the category tree and
item details: side by side in wide windows, stacked in narrow windows. Searching and selecting only affect viewing,
not the migration scope. Actions and the safety reminder stay visible. Item details, tooltips and the report show the same scan and copy diagnostics; tooltips and the report also
retain full paths.

Window sizing follows the category preset workflow: 720×480 logical pixels for source and result pages, 1160×780 for preview
and expanded result details, centered, without growing on 4K screens.
Smaller screens cap it at the work area minus 40 pixels horizontally and 60 vertically. Long lists and details scroll independently;
file version choices stay above long descriptions. Numeric cards show Mods,
character/subject nodes, regular categories and excluded items without counting subjects as regular categories.
Searches do not alter these counts. Type and status have separate columns, and the two panels align at the bottom.

The plugin preserves supported category and object assignments, names, authors, descriptions, ratings, tags,
the first valid source address, and readable local previews. Disabled indexes are included only when their local
original archives exist. SHA and readable filename index keys are supported. Active metadata takes precedence over disabled entries with the same index key, with a warning;
conflicts within the same enabled/disabled state and ambiguous category assignments are still refused. Missing
resources and invalid indexes remain visible as issues. Index-only and remote-only entries are not imported, and
nothing is downloaded. Imported mods stay disabled. Deployment paths, enabled states, loader hotkeys, and legacy
app settings are excluded. No standalone shortcut or loader configuration migration is provided; selected cache
files, including modified Mod INIs, are copied naturally. Legacy files are preserved. Estimated source size may be
smaller than the space needed for extracted files and previews.

Compatibility follows the public data format at legacy commit `b5f76a6844887e273978a9643c92046844d3b117`.
Category wildcard rules and relative thumbnail redirection are supported. Exact thumbnails take precedence, with
the legacy `key-in only`, `similarity only`, `similarity threshold`, and `similarity/key-in` modes available according
to the old configuration. Approximate matches carry a review warning; unknown modes fall back to exact matching
with a warning. Known layouts include SHA-named images and SHA subfolders in shared `resources/preview` and
full-screen `resources/preview_screen`; shared SHA directories are dedicated preview folders accepting all supported
images. Active and disabled caches are searched recursively for images whose names start with `preview`, and all
supported images inside explicit `preview`/`preview_screen` subfolders, including nested folders. PNG, JPG, JPEG, BMP,
GIF, and WebP are supported and deduplicated by image bytes. There is no arbitrary texture sweep or overwrite of
existing Plus Mod metadata or previews. Custom storage from third-party legacy plugins, symbolic links, and
junctions are outside the supported input format.

### Archive and cache recovery

The original archive is preferred and its file SHA-1 is checked by the host before import. The scanner also checks
`work/Mods/<SHA>` and `work/Mods/disabled-<SHA>` in the selected environment. Multiple possible caches are ignored
with a warning rather than choosing an uncertain source. When an archive and a valid cache may differ, select the
Mod row and choose **Original files (recommended)** or **Edited files**. Differences or an
unreliable comparison produce a warning; recovering cache modifications is an explicit user choice.

An active index with a valid cache but no original archive can be recovered with an explicit incompleteness warning;
check the result afterwards. Disabled cache-only or remote-only entries remain excluded. A separate directory
SHA-256 fingerprint covers relative paths and file contents and is checked again before and after host preparation.
Changes fail the import and require a new scan. This fingerprint is neither the original archive SHA-1 nor a Plus Mod ID.
For readable index keys, the scanner computes the actual archive SHA-1 instead of treating the key as a digest. Reports
show both values separately; cache-only recovery does not invent an archive digest. Paths, device names, alternate
data streams, and Windows filename aliases remain rejected.

### Stopping, duplicates, and reports

Stopping from this window or the host task center ends the remaining queue at a safe boundary. An item already
committed or identified as a duplicate keeps its successful or skipped result. Completed imports survive a stop,
window closure, or plugin deactivation. Switching games invalidates the previous
preview. Migration history records the target game, source SHA, and actual target Mod ID; every recorded target is
checked before skipping. Cache history additionally uses its directory SHA-256 fingerprint, so an archive history
entry does not hide different cache modifications. The host also detects duplicate content and preserves existing
metadata and previews. Legacy SHA values validate original archives and are never used as Plus Mod IDs or content fingerprints.

At the end of each copy or retry batch, the host creates a unique UTF-8 BOM `.txt` file in the system temporary
directory. Closing the wizard or switching games during copying also opens an interruption
report. Completed results are preserved, unstarted items are cancelled, and operations without a final host result
remain explicitly in progress; check the host library and task center for their final outcome. The report opens with
the default application. It includes target game identity, selected sources, original SHAs,
cache fingerprints, target Mod IDs, duplicate-skip reasons, all outcomes, and scan/copy diagnostics. **Open
report** generates and opens another report after completion. If the default application cannot open it, the file
remains and a host notification shows its full path. Report creation failures do not undo successful imports; window
results remain available. Reports survive window closure and plugin deactivation, but the system may clean temporary
files according to its own policy; save a separate copy for long-term retention.

The plugin uses host-managed background tasks, serial SDK imports, and host theme bindings. It requests only
`mods:read`, `mods:write`, `fs:read`, `storage`, `background`, `ui:views`, and `ui:tools-page`. It does not use network
access or run legacy code. `ctx.ui.show_text_report(text, title="") -> str` delegates report saving and opening to the
host using the existing `ui:views` permission, with no additional permissions, dependencies, or API version change.
Window closure releases observers and timers and suppresses stale callbacks.

Locked output files receive limited cleanup retries. Remaining output is preserved with a receipt under
`data/recovery/import-cleanup/` and a path diagnostic; restarts and index rebuilds keep it out of ordinary Mod discovery.
Later imports retry cleanup. Uncertain commits, changed identities, and externally added files are preserved for
inspection. A successfully imported item can still carry a cleanup error, so also review its result details.

Blocked objects and warnings also appear in the tree with Cannot import or Needs attention statuses.
Notes follow an item only when placement is unambiguous; other rows use an unresolved-placement group.
These display-only rows never add imports. Select a tree row to read its recorded diagnostic in item details.
