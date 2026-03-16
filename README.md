# d3dxSkinManage-Plus

基于 PySide6 的 Windows 桌面 Mod 管理器，用于管理 3DMigoto 游戏资源、分类、预设和在线下载。
项目尚未正式发布，采用非商业的源码可用许可证。

## 功能

- 按游戏独立管理资源目录、加载器和游戏启动路径。
- 导入 ZIP、7z、RAR 和目录，按类别浏览、搜索、编辑标签及预览图片。
- 启用、禁用和部署 Mod，保存组合预设与单个 Mod 的外观变体。
- 监控资源目录变化，校验受管文件身份，重建索引并恢复未完成的文件操作。
- 浏览 GameBanana、下载 Mod、检查并处理 Mod 更新。
- 配置主题、背景、语言包与快捷键，通过 Python 插件扩展命令、页面和操作。

程序初次启动时没有游戏配置，需要自行添加。在线内容按用户填写的游戏英文名查询，
匹配结果保存在本地；修改英文名后会重新匹配。

## 从源码运行

需要 Windows 10/11 x64、Python 3.10–3.14。依赖版本和平台条件统一定义于
[pyproject.toml](pyproject.toml)。在项目根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe run_app.py
```

也可以通过 `python -m src.main` 启动。`requirements.txt` 保留为同一安装配置的入口。
源码安装用于在当前仓库运行；Windows 发行包通过下述构建流程生成。

使用发行版时，可双击 `docs/d3dxSkinManage-Plus 使用教程.html` 离线阅读；源码文档见[教程入口](docs/tutorials/README.md)。
程序运行时会创建 `config/`、`data/`、`cache/`
和 `logs/`；个人配置、Mod、数据库及下载缓存不参与版本管理。

## 开发与验证

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe scripts/quality/check_repository.py
.venv\Scripts\python.exe scripts/i18n/i18n_catalog.py --check
.venv\Scripts\python.exe -m pytest
```

代码与注释规范见[贡献指南](CONTRIBUTING.md)，测试和性能验证见[测试说明](tests/README.md)。
默认语言包的编辑方法见[语言包指南](config/languages/README.md)。

## 构建 Windows 发行包

主程序使用 Nuitka，原生更新器使用 Visual Studio 2022 的 C++17 工具链。
先按[更新器说明](updater/README.md)构建更新器，再运行：

```powershell
.venv\Scripts\python.exe scripts/release/build.py --install-deps --with-updater --zip
```

也可双击根目录的 `构建工具.bat`。构建产物位于 `build/`，详细选项见
[构建指南](docs/构建指南.md)。公开发行前需配置自己的更新源；仓库不会假定一个尚未确定的 GitHub 地址。

## 插件开发

```powershell
.venv\Scripts\python.exe scripts/plugins/new_plugin.py com.example.mod-summary --name "Mod 统计助手"
.venv\Scripts\python.exe scripts/plugins/plugin_tool.py check plugins/com.example.mod-summary
```

插件从应用根目录的 `plugins/` 加载，首次启用需要确认信任和权限。
随包提供 Mod 清单导出、预设检查和预设轮换三个实用教学示例。
SDK 2.0 支持多文件包、按需激活、版本依赖、可取消后台任务、可释放资源和可观察配置。
脚手架生成可直接运行的多文件插件；接口、打包与验证步骤见
[插件开发指南](docs/tutorials/zh/插件开发指南.md)。

## 目录

| 路径 | 用途 |
| --- | --- |
| `src/core/`、`src/models/` | 资源管理、持久化、操作事务与数据模型 |
| `src/services/` | 压缩、图像、网络和更新服务 |
| `src/ui/` | PySide6 界面、后台任务与交互组件 |
| `src/plugin_sdk/`、`src/plugins/` | 插件公开接口与宿主运行时 |
| `resources/` | 主题、图标、署名素材与项目短片 |
| `config/languages/` | 默认中文、英文语言包及格式说明 |
| `plugins/` | 随包插件示例与本地安装位置 |
| [`scripts/`](scripts/README.md) | 按职责分类的构建、质量检查、语言包、插件、资源、启动和性能工具 |
| `updater/` | 原生 C++ 更新器及其测试 |
| `tests/` | 自动化测试与人工验证说明 |
| `docs/` | 用户与开发者文档 |

## 许可证与第三方资源

本项目采用 **PolyForm Noncommercial License 1.0.0 与署名完整性附加条款**。
非商业使用、修改和分发须遵守 [LICENSE](LICENSE)，并保留要求的署名。
该许可不属于 OSI 认可的开源许可证。

第三方资源保持其原有许可，见[第三方声明](THIRD_PARTY_NOTICES.md)。

## 独立视频资源

从源码制作完整发行包前，按[短片资源说明](resources/about/story/README.md)准备独立视频资源。
