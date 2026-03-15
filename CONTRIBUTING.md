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

## 代码规范

- 文件使用 UTF-8，缩进使用空格。Python、C++、文档使用 LF；Windows 脚本和工程文件使用 CRLF。
- Python 使用 Ruff 0.17.2，行宽为 120，字符串默认使用双引号。
- Python 标识符遵循 PEP 8，类型使用 `list[str]`、`dict[str, int]`、`str | None` 等现代写法。
  Qt 重写方法和外部协议字段保留接口规定的名称。
- C++ 使用 C++17 和 clang-format 18.1.8，具体规则由根目录 `.clang-format` 定义。
- 注释与文档字符串使用简明、正式的中文，说明业务约束、算法理由、线程边界、资源所有权或接口契约。
  技术标识符、工具指令、法律声明和必要的示例代码保留原文。
- 维护的 Python 模块、类和函数须提供中文文档字符串，包括私有方法、嵌套回调及协议接口。
  测试重点说明夹具、辅助对象和复杂场景；名称已完整表达预期行为的简单测试无需重复说明。
  批量编辑中文时须明确文件与管道编码，并在写入后检查内容，避免产生连续问号或替代字符。
- 不在代码注释中记录开发编号、设计定稿、阶段交付、对话过程或实现参照；不保留口语、感叹、装饰分隔线和注释掉的废弃代码。
- 源码、文档、默认配置、测试素材及文件名不使用真实游戏、游戏厂商、角色名称或无关第三方项目类比。
  示例采用中性名称。必要的依赖名称、外部接口字段、法律原文及署名保持准确。
- 语言包独立维护，校验其 JSON 格式、编码和占位符。
- 用户可见文本通过现有翻译接口声明；语言包键、持久化字段与外部接口字段不得作为文案随意替换。
- 删除代码前检查调用关系，包括 Qt 信号、插件入口、子进程入口和动态注册。
  项目尚未发布，不为废弃的内部格式保留迁移层；平台差异、外部文件格式和事务恢复仍需按实际契约处理。

自动整理格式：

```powershell
.venv\Scripts\python.exe -m ruff check --fix .
.venv\Scripts\python.exe -m ruff format .
Get-ChildItem updater -Recurse -File -Include *.cpp,*.h |
    ForEach-Object { .venv\Scripts\clang-format.exe -i $_.FullName }
```

## 验证修改

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe scripts/quality/check_repository.py
.venv\Scripts\python.exe scripts/i18n/i18n_catalog.py --check
.venv\Scripts\python.exe -m pytest
```

修复行为问题时，优先补充能复现问题的测试，验证真实输入、输出和持久化结果。
仓库质量检查覆盖源码说明、中文文档完整性、维护文本的 UTF-8 编码、文档链接及 C++ 格式。
维护脚本按职责放在 `scripts/` 的子目录，入口见[脚本索引](scripts/README.md)。
大库启动脚本在 `scripts/launch/` 中维护，测试数据使用仓库外的独立目录。
共享测试夹具用显式同名导入重新导出，例如 `from tests.helpers import fixture_name as fixture_name`，
避免将夹具注册误当成未使用导入。Qt 测试必须清理窗口、定时器、工作线程和未投递的回调。

C++ 修改还需完成[更新器构建与测试](updater/README.md)。界面、桌面合成和性能修改的验证方法见
[测试说明](tests/README.md)。

## 提交与审查

一次提交集中解决一个问题。提交说明写清触发条件、修改后的行为和实际验证结果。
涉及格式、文件结构或接口清理时说明删除依据；涉及配置和数据时说明失败后的处理方式。
不要提交会话记录、开发计划、临时截图或第三方源码副本。

新增用户功能时更新对应用户文档；新增插件接口时同步更新插件指南和随包示例。
第三方资源的来源及许可记录在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
