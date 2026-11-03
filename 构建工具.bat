@echo off
setlocal

if defined D3DX_BUILD_MENU_UTF8 goto utf8_ready
set "D3DX_BUILD_MENU_UTF8=1"
chcp 65001 >nul
"%ComSpec%" /d /s /c ""%~f0" %*"
exit /b %errorlevel%

:utf8_ready
REM 使用独立的 UTF-8 CMD，避免从 CP936 启动时截断中文命令。
set "D3DX_BUILD_MENU_UTF8="
cd /d "%~dp0"
title d3dxSkinManage-Plus 构建工具

REM 常规构建使用 build.py，版本管理与完整发行使用 release.py，成品保存到项目 build 目录。
REM 主程序使用 Nuitka，原生更新器使用 updater/tools/build.py。
REM 可通过 PYTHON 环境变量指定构建解释器。

set "ROOT=%~dp0"

set "PY="
if not "%PYTHON%"=="" set "PY=%PYTHON%"
if not defined PY if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"
if not defined PY set "PY=python"

"%PY%" --version >nul 2>&1
if errorlevel 1 goto no_python

REM 带参数时直接执行非交互构建。
if not "%~1"=="" goto passthrough

:menu
set "OUTPUT_HINT=%ROOT%build\"
cls
echo ============================================================
echo            d3dxSkinManage-Plus  -  构建工具菜单
echo ------------------------------------------------------------
echo   构建解释器: %PY%
echo   成品输出  : %ROOT%build\
echo ============================================================
echo.
echo  [1] 标准构建      standalone 文件夹（启动快，推荐）
echo  [2] 单文件构建    onefile 单个 exe（便于分发，启动略慢）
echo  [3] 调试构建      standalone + 保留控制台（闪退排错用）
echo  [4] 发行构建      输入版本号，自动编译主程序和更新器并打包
echo  [5] C++ 更新器    自动编译原生 Release x64 更新器
echo.
echo  [6] 检查构建环境  验证 Nuitka 和运行依赖
echo  [7] 配置构建环境  安装构建和运行依赖
echo  [8] 清理构建输出  删 build\ 里的成品 + 编译暂存目录
echo  [9] 打开输出目录
echo  [V] 修改版本号    同步主程序、Python 包和更新器，不编译
echo  [0] 退出
echo.
echo ============================================================
echo.

set "choice="
set /p choice=请选择操作 [0-9/V]:

if "%choice%"=="1" goto build_standalone
if "%choice%"=="2" goto build_onefile
if "%choice%"=="3" goto build_debug
if "%choice%"=="4" goto build_release
if "%choice%"=="5" goto build_updater
if "%choice%"=="6" goto check_env
if "%choice%"=="7" goto setup_env
if "%choice%"=="8" goto clean_output
if "%choice%"=="9" goto open_output
if /i "%choice%"=="V" goto change_version
if "%choice%"=="0" goto end

echo.
echo 无效的选择，请重新输入。
"%SystemRoot%\System32\timeout.exe" /t 2 >nul
goto menu

:build_standalone
cls
echo ============================================================
echo  标准构建（standalone 文件夹）
echo  首次编译要几分钟，Nuitka 可能联网下载编译器和依赖分析器。
echo ============================================================
echo.
"%PY%" scripts\release\build.py
set "RC=%errorlevel%"
goto done

:build_onefile
cls
echo ============================================================
echo  单文件构建（onefile）
echo  产物是一个 exe，运行时先自解压到临时目录，启动比 standalone 慢。
echo ============================================================
echo.
"%PY%" scripts\release\build.py --onefile
set "RC=%errorlevel%"
goto done

:build_debug
cls
echo ============================================================
echo  调试构建（standalone + 保留控制台）
echo  目标机上闪退/不出窗口时用这个，从控制台看 Python 报错。
echo ============================================================
echo.
"%PY%" scripts\release\build.py --console
set "RC=%errorlevel%"
goto done

:build_release
cls
echo ============================================================
echo  发行构建（输入一次版本号，自动完成全部步骤）
echo    1. 同步主程序、Python 包和更新器版本
echo    2. 自动编译原生更新器与 standalone 主程序
echo    3. 生成发行 ZIP 和 SHA256  ==^>  build\release\
echo  版本号留空时使用当前源码版本。
echo ============================================================
echo.
"%PY%" -X utf8 scripts\release\release.py --interactive
set "RC=%errorlevel%"
set "OUTPUT_HINT=%ROOT%build\release\"
goto done

:build_updater
cls
echo ============================================================
echo  C++17 原生 x64 更新器
echo  自动查找 Visual Studio 或 Build Tools 的 C++ 工具集。
echo  产物: updater\bin\x64\Release\updater.exe
echo ============================================================
echo.
"%PY%" -X utf8 updater\tools\build.py --updater-only
set "RC=%errorlevel%"
set "OUTPUT_HINT=%ROOT%updater\bin\x64\Release\"
goto done

:change_version
cls
echo ============================================================
echo  修改源码版本号（不执行编译）
echo  同步主程序、Python 包、更新器资源与清单中的版本。
echo ============================================================
echo.
"%PY%" -X utf8 scripts\release\release.py --interactive --prepare-only
set "RC=%errorlevel%"
echo.
if not "%RC%"=="0" echo [X] 修改版本失败（退出码 %RC%），请查看错误输出。
echo.
pause
goto menu

:check_env
cls
echo ============================================================
echo  检查构建环境
echo ============================================================
echo.
"%PY%" scripts\release\build.py --check
if errorlevel 1 (
    echo.
    echo 环境不完整，请选择菜单 [7] 安装构建依赖。
)
echo.
pause
goto menu

:setup_env
cls
echo ============================================================
echo  配置构建环境（首次使用或更换环境时执行）
echo    将构建和运行依赖安装到当前解释器
echo    默认使用 PyPI；安装后自动检查环境。
echo ============================================================
echo.
"%PY%" scripts\release\build.py --install-deps --check
echo.
pause
goto menu

:clean_output
cls
echo ============================================================
echo  清理构建输出（build\ 里的成品 + 编译暂存目录）
echo ============================================================
echo.
"%PY%" scripts\release\build.py --clean
echo.
pause
goto menu

:open_output
if not exist "%ROOT%build" mkdir "%ROOT%build"
explorer "%ROOT%build"
goto menu

:done
echo.
if "%RC%"=="0" (
    echo ============================================================
    echo  [OK] 完成，成品在  %OUTPUT_HINT%
    echo ============================================================
) else (
    echo ============================================================
    echo  [X] 构建失败（退出码 %RC%），请查看错误输出。
    echo      缺 Nuitka / 缺依赖  ==^>  选择菜单 [7] 配置构建环境
    echo      链接器报 LNK1104    ==^>  选择菜单 [8] 清理后重试
    echo ============================================================
)
echo.
pause
goto menu

:passthrough
"%PY%" scripts\release\build.py %*
exit /b %errorlevel%

:no_python
echo.
echo [X] 找不到可用的 Python：%PY%
echo     解决：安装 Python 后重试，或先执行
echo           set PYTHON=完整\路径\python.exe
echo.
pause
exit /b 1

:end
echo.
echo 再见。
"%SystemRoot%\System32\timeout.exe" /t 1 >nul
exit /b 0
