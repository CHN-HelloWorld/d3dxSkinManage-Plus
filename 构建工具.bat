@echo off
cd /d "%~dp0"
chcp 65001 >nul
title d3dxSkinManage-Plus 构建工具

REM 构建菜单将参数传递给 scripts/release/build.py，成品保存到项目 build 目录。
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
echo  [4] 发行构建      标准构建 + 更新器 + 打包 zip
echo  [5] C++ 更新器    查看 Visual Studio 构建入口
echo.
echo  [6] 检查构建环境  验证 Nuitka 和运行依赖
echo  [7] 配置构建环境  安装构建和运行依赖
echo  [8] 清理构建输出  删 build\ 里的成品 + 编译暂存目录
echo  [9] 打开输出目录
echo  [0] 退出
echo.
echo ============================================================
echo.

set "choice="
set /p choice=请选择操作 [0-9]:

if "%choice%"=="1" goto build_standalone
if "%choice%"=="2" goto build_onefile
if "%choice%"=="3" goto build_debug
if "%choice%"=="4" goto build_release
if "%choice%"=="5" goto build_updater
if "%choice%"=="6" goto check_env
if "%choice%"=="7" goto setup_env
if "%choice%"=="8" goto clean_output
if "%choice%"=="9" goto open_output
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
echo  发行构建（请先在 VS 中生成 Release x64 更新器）
echo    1. standalone 主程序   ==^>  build\d3dxSkinManage-Plus\
echo    2. 更新器 updater.exe  ==^>  成品的 Updater\ 子目录
echo    3. 打包发布压缩包      ==^>  build\*-win64.zip
echo ============================================================
echo.
"%PY%" scripts\release\build.py --with-updater --zip
set "RC=%errorlevel%"
goto done

:build_updater
cls
echo ============================================================
echo  C++17 原生 x64 更新器 - Visual Studio 2022
echo  打开: %ROOT%updater\updater.sln
echo  选择 Release ^| x64，然后生成解决方案。
echo  需要 MSVC v143 和 Windows 10/11 SDK。
echo  产物: Updater\bin\x64\Release\updater.exe
echo  自动复制到 build\Updater\updater.exe
echo ============================================================
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
    echo  [OK] 完成，成品在  %ROOT%build\
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
