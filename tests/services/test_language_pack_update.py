"""验证宿主翻译模块导入不加载界面。

原生更新器的语言配置由 updater/tests 中的对应测试覆盖。
"""

import json
import subprocess
import sys


def test_standalone_catalog_does_not_import_host_or_qt_dependencies():
    command = (
        "import json, sys; import src.utils.translation_catalog, src.utils.constants, src.utils.fs; "
        "print(json.dumps([name for name in sys.modules "
        "if name in ('src.utils.logger', 'src.utils.paths', 'src.utils.i18n') "
        "or name.startswith('PySide6')]))"
    )
    result = subprocess.run([sys.executable, "-c", command], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
