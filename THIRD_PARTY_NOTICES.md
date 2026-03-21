# 第三方声明

项目自身的许可见 [LICENSE](LICENSE)。第三方组件和资源保留各自许可，不因包含在本项目中而变更。

## Fluent UI System Icons

`resources/icons/fluent/` 中的图标来自
[Microsoft Fluent UI System Icons](https://github.com/microsoft/fluentui-system-icons)，
资源维护脚本为 `scripts/assets/fetch_fluent_icons.sh`。许可原文如下：

```text
MIT License

Copyright (c) 2020 Microsoft Corporation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 运行依赖

以下为直接依赖的许可索引；实际分发需同时保留所选版本及其传递依赖的许可文件。

| 组件 | 许可 |
| --- | --- |
| PySide6 / Qt | LGPL-3.0、GPL-2.0 或 GPL-3.0，具体模块以其许可为准 |
| windows-capture | MIT |
| py7zr | LGPL-2.1-or-later |
| rarfile | ISC |
| unrar2-cffi | Apache-2.0；其 UnRAR 引擎另受 RARLAB 的 UnRAR 许可约束 |
| watchdog | Apache-2.0 |
| Pillow | MIT-CMU |
| httpx | BSD-3-Clause |
| pypinyin | MIT |
| bleach | Apache-2.0 |
| tinycss2 | BSD-3-Clause |
| tenacity | Apache-2.0 |
| RapidFuzz | MIT |

发行包的 `licenses/` 保存所选运行依赖及其传递依赖的许可文件、Python 运行时许可与来源索引。
仓库在 `resources/licenses/` 维护 wheel 未完整携带的补充原文：Qt 的 LGPL-3.0、GPL-2.0、GPL-3.0
及 Qt GPL Exception 1.0 取自
[Qt for Python 6.11.1 官方源码](https://github.com/pyside/pyside-setup/tree/v6.11.1/LICENSES)；
UnRAR 引擎许可取自
[unrar2-cffi 0.5.0 源码发行包](https://files.pythonhosted.org/packages/12/e2/da7af34f34dd4c9baa92253266a153c3b2b479a07fde56cd620c732617fe/unrar2_cffi-0.5.0.tar.gz)
内的 `unrarsrc/license.txt`。补充文件保留上游原文，仅随对应组件进入发行包。

UnRAR 仅用于解压，不提供 RAR 压缩功能。发行者应遵守
[RARLAB 许可](https://www.rarlab.com/rar_add.htm)对压缩算法用途的限制。

关于页中的作者、贡献者署名与相关素材应保持原有用途；不应从程序的非商业许可推断获得
第三方商标、人物肖像或其他独立素材的额外授权。
