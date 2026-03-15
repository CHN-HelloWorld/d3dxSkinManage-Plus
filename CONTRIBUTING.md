# 贡献指南

提交修改前请阅读 [LICENSE](LICENSE)。项目采用非商业的源码可用许可，第三方代码和资源须保留原有许可及署名。

## 开发环境

使用 Windows 10/11 x64 和 Python 3.10–3.14，在仓库根目录创建虚拟环境并安装开发依赖：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

依赖仅在 `pyproject.toml` 声明；`requirements.txt` 引用同一配置。不要提交虚拟环境、数据库、
个人配置、日志、下载包或构建产物。临时调查资料应放在系统临时目录。

