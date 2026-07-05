# 使用教程与插件开发指南

教程覆盖首次配置、分类与 Mod 管理、组合预设、变体、在线更新、设置、备份及插件开发。
截图来自实际程序界面，演示库使用合成名称和占位资源。

| 内容 | 简体中文 | English |
| --- | --- | --- |
| 第一次导入和启用 | [5 分钟快速上手](zh/5分钟快速上手.md) | [5-minute quick start](en/Quickstart.en.md) |
| 按功能查阅 | [完整功能手册](zh/完整功能手册.md) | [Complete user manual](en/Manual.en.md) |
| SDK 2.0 与插件开发 | [插件开发指南](zh/插件开发指南.md) | [Plugin development guide](en/PluginGuide.en.md) |

## 离线阅读

发行程序的 `docs/` 目录包含以下四份 HTML：

- `d3dxSkinManage-Plus 使用教程.html`
- `d3dxSkinManage-Plus User Guide.html`
- `d3dxSkinManage-Plus 插件开发指南.html`
- `d3dxSkinManage-Plus Plugin Development Guide.html`

双击任一文件即可在浏览器中阅读。每份文件内嵌中英文正文、图片和阅读脚本，可独立复制。
页眉切换语言时保留当前小节；目录支持筛选，配图支持查看原始尺寸。插件指南中引用的源码、
Schema 内嵌为可展开的附录，阅读这些引用不需要另带项目源码。

构建指南和教程制作工具属于开发资料，保留在源码仓库，不进入发行程序的 `docs/`。

## 维护来源

| 内容 | 维护位置 |
| --- | --- |
| 快速上手 | `zh/5分钟快速上手.md`、`en/Quickstart.en.md` |
| 八章手册及附录 | `chapters/zh/`、`chapters/en/` |
| 插件公开接口与示例 | `zh/插件开发指南.md`、`en/PluginGuide.en.md` |
| 原始界面截图与控件坐标 | `images/final/`、`images/final-en/` |
| 阅读配图与标注记录 | `images/guide/`、`images/guide-en/` |
| 教程插画 | `images/plusa/` |
| 生成器、截图与核验工具 | `scripts/tutorials/`（仓库根目录下） |

`zh/完整功能手册.md` 与 `en/Manual.en.md` 由章节源生成，方便直接在源码仓库阅读；修改章节后重新生成，
不要独立维护这两份汇总文件。两种语言的标题层级、小节编号、表格和示例应同步更新。
插件指南分别在 `zh/` 和 `en/` 中维护。

## 生成与核验

在仓库根目录执行：

```powershell
& ".venv/Scripts/python.exe" -X utf8 -B "scripts/tutorials/build.py"
& ".venv/Scripts/python.exe" -X utf8 -B "scripts/tutorials/verify.py"
```

默认生成到 `build/tutorials/`。可以向两个命令传入相同的 `--output-dir`，使用其他产物目录。
生成器会重新拼装手册，核对中英文标题对应关系，并内嵌原始尺寸的无损图片。
核验工具检查正文结构、本地引用、双向语言导航、截图标注、图片像素和当前 SDK 版本。

程序构建会先重新生成教程，再将这四份 HTML 放入成品 `docs/`，同时写入发行 ZIP 和更新清单。
完整程序的构建方法见[构建指南](../构建指南.md)。

## 更新截图

以下操作使用系统临时目录中的专用演示库，不读取个人 Mod 资源，也不访问真实下载服务：

```powershell
& ".venv/Scripts/python.exe" -X utf8 -B "scripts/tutorials/prep_workspace.py"
& ".venv/Scripts/python.exe" -X utf8 -B "scripts/tutorials/capture_main.py"
& ".venv/Scripts/python.exe" -X utf8 -B "scripts/tutorials/capture_main.py" --language en-US
& ".venv/Scripts/python.exe" -X utf8 -B "scripts/tutorials/build_guides.py"
```

截图按两倍分辨率渲染。仅更新部分操作时使用 `--only "17,18"`；场景编号及控件位置记录在
`images/final/captures.json` 和 `images/final-en/captures.json`。界面布局变化后检查
`scripts/tutorials/guide_layouts.json` 与 `scripts/tutorials/guide_layouts.en.json`，再生成配图和离线 HTML。
