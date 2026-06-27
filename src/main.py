"""d3dxSkinManage-Plus 入口"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

# 将项目根目录加入 sys.path，使 src 包可被导入
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def main() -> int:
    """程序主入口"""
    if len(sys.argv) > 1 and sys.argv[1] == "--background-renderer":
        if len(sys.argv) != 4:
            return 2
        from src.ui.backgrounds.render_worker import main as renderer_main

        return renderer_main(sys.argv[2], int(sys.argv[3]))

    if len(sys.argv) > 1 and sys.argv[1] == "--mod-update-worker":
        if len(sys.argv) != 3:
            return 2
        from src.core.mod_update_worker import main as worker_main

        return worker_main(sys.argv[2])

    # 在导入应用前初始化日志，保留启动期间的异常信息。
    try:
        # 控制台快速编辑模式会阻塞标准输出，需在日志写入前禁用。
        from src.utils.win_console import disable_quick_edit_mode

        disable_quick_edit_mode()

        from src.utils.logger import init_app_logging

        init_app_logging()
    except Exception:
        traceback.print_exc()
    try:
        from src.app import App

        app = App.instance()
        return app.run()
    except Exception as e:
        try:
            from src.utils.logger import logger

            logger.critical("程序启动失败", exc_info=True)
        except Exception:
            pass
        traceback.print_exc()
        print(f"\n程序启动失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
