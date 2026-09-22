"""使用本机 MSVC 构建更新器、原生测试和离屏预览工具。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def main() -> None:
    """发现本机 MSVC 工具集并依次构建更新器、预览工具和原生测试，任一构建失败时停止。"""
    root = Path(__file__).resolve().parents[2]
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / (
        "Microsoft Visual Studio/Installer/vswhere.exe"
    )
    if not vswhere.is_file():
        raise SystemExit("请安装 Visual Studio 或 Build Tools，并选择 C++ 桌面开发组件。")
    installation = subprocess.check_output(
        [
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.Component.MSBuild",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
            "-utf8",
        ],
        text=True,
        encoding="utf-8",
    ).strip()
    if not installation:
        raise SystemExit("未找到可用的 Visual Studio C++ 构建工具。")
    visual_studio = Path(installation)
    msbuild = visual_studio / "MSBuild/Current/Bin/MSBuild.exe"
    toolsets = {
        path.name
        for path in (visual_studio / "MSBuild/Microsoft/VC").glob("v*/Platforms/x64/PlatformToolsets/v*")
        if path.is_dir()
    }
    if not toolsets:
        raise SystemExit("未找到 x64 C++ 平台工具集。")
    toolset = "v143" if "v143" in toolsets else max(toolsets)
    # MSBuild 不接受大小写不同的重复环境变量，仅为构建子进程规范化变量名。
    environment = {key.upper(): value for key, value in os.environ.items()}
    for project in (
        "updater/updater.vcxproj",
        "updater/tools/capture_ui.vcxproj",
        "updater/tests/updater_tests.vcxproj",
    ):
        subprocess.run(
            [
                str(msbuild),
                project,
                "/p:Configuration=Release",
                "/p:Platform=x64",
                f"/p:PlatformToolset={toolset}",
                "/p:DeployUpdater=false",
                "/m:1",
                "/nodeReuse:false",
                "/v:minimal",
                "/nologo",
            ],
            cwd=root,
            env=environment,
            check=True,
        )


if __name__ == "__main__":
    main()
