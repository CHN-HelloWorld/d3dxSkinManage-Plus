"""源码运行与 Nuitka 构建入口。

入口位于仓库根目录，确保构建器能够解析 src 包。
"""

from src.main import main

if __name__ == "__main__":
    raise SystemExit(main())
