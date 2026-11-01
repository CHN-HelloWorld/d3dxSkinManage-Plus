# 脚本索引

脚本按职责分目录维护，专用配置与使用它的脚本放在一起。下列命令均从仓库根目录执行；
Windows 使用 `.venv\Scripts\python.exe`，其他平台可替换为当前环境的 Python。

| 目录 | 职责与入口 |
| --- | --- |
| `release/` | [build.py](release/build.py) 构建和打包发行文件；[github_release.py](release/github_release.py) 准备云端发行版本、独立视频资源和发布附件；[release_licenses.py](release/release_licenses.py) 收集运行依赖许可；[nuitka-package.config.yml](release/nuitka-package.config.yml) 配置 Nuitka 包处理规则。 |
| `tutorials/` | [build.py](tutorials/build.py) 生成离线教程，由发行构建自动调用；[verify.py](tutorials/verify.py) 核验文档、配图与语言导航；截图和标注工具用于维护教程素材。 |
| `quality/` | [check_repository.py](quality/check_repository.py) 检查文本编码、源码说明、文档链接和 C++ 格式。 |
| `i18n/` | [i18n_catalog.py](i18n/i18n_catalog.py) 提取翻译原文、创建语言包并校验占位符。 |
| `plugins/` | [new_plugin.py](plugins/new_plugin.py) 创建插件骨架；[plugin_tool.py](plugins/plugin_tool.py) 校验和打包插件。 |
| `assets/` | [make_app_icon.py](assets/make_app_icon.py) 生成多尺寸 ICO；[fetch_fluent_icons.sh](assets/fetch_fluent_icons.sh) 获取 Fluent 图标资源。 |
| `launch/` | [run_as_admin.py](launch/run_as_admin.py) 通过 Windows UAC 启动应用；[open_experience.py](launch/open_experience.py) 使用当前源码打开已有的独立大库环境。 |
| `performance/` | 合成库、界面响应及图像缓存的性能测量，入口见下表。 |

## 性能脚本

| 入口 | 测量范围 |
| --- | --- |
| [perf_mod_library.py](performance/perf_mod_library.py) | 不同规模合成库的核心操作、GUI 场景和导入流程。 |
| [perf_mod_library_scenarios.py](performance/perf_mod_library_scenarios.py) | 合成库中的组合操作场景及界面响应。 |
| [perf_mod_library_shapes.py](performance/perf_mod_library_shapes.py) | 不同文件结构和内容大小的 Mod 库操作。 |
| [perf_rebuild_responsiveness.py](performance/perf_rebuild_responsiveness.py) | 索引重建耗时及界面心跳。 |
| [benchmark_card_scroll.py](performance/benchmark_card_scroll.py) | 本地与在线卡片的滚动。 |
| [benchmark_card_preview_return.py](performance/benchmark_card_preview_return.py) | 本地预览滚动返回后的图像就绪时间和重复解码。 |
| [benchmark_online_preview_return.py](performance/benchmark_online_preview_return.py) | 在线预览滚动返回后的图像就绪时间和缓存行为。 |
| [benchmark_image_memory_lifecycle.py](performance/benchmark_image_memory_lifecycle.py) | 图像缓存、窗口切换与关闭后的内存生命周期。 |

## 常用命令

```powershell
.venv\Scripts\python.exe scripts/release/build.py --dry-run
.venv\Scripts\python.exe scripts/release/github_release.py pack-media
.venv\Scripts\python.exe scripts/tutorials/build.py
.venv\Scripts\python.exe scripts/tutorials/verify.py
.venv\Scripts\python.exe scripts/quality/check_repository.py
.venv\Scripts\python.exe scripts/i18n/i18n_catalog.py --check
.venv\Scripts\python.exe scripts/plugins/new_plugin.py com.example.mod-summary --name "Mod 统计助手"
.venv\Scripts\python.exe scripts/plugins/plugin_tool.py check plugins/com.example.mod-summary
.venv\Scripts\python.exe scripts/assets/make_app_icon.py "D:/Artwork/app.png"
.venv\Scripts\python.exe scripts/launch/open_experience.py --data-root "D:/TestLibraries/large-library"
.venv\Scripts\python.exe scripts/performance/perf_mod_library.py --help
```

Fluent 图标获取脚本使用 Bash 执行：`bash scripts/assets/fetch_fluent_icons.sh`。
性能脚本的参数通过各自的 `--help` 查看；测试数据和输出使用独立目录。

详细说明见[构建指南](../docs/构建指南.md)、[语言包维护](../docs/tutorials/zh/语言包指南.md)、
[插件开发指南](../docs/tutorials/zh/插件开发指南.md)和[测试说明](../tests/README.md)。
