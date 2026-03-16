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

