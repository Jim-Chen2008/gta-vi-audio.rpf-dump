@echo off
chcp 65001 >nul
setlocal

rem ===== 改这两行就行 =====
set "AUDIO_RPF=C:\迅雷下载\GTA6.Xbox.v1.0.2.1-CYBERLEEK\audio.rpf"
set "OUT_DIR=D:\music_all"
rem ========================

if not exist "%AUDIO_RPF%" (
    echo [!] 找不到 audio.rpf: %AUDIO_RPF%
    echo     请编辑本文件的 AUDIO_RPF 变量。
    pause
    exit /b 1
)

set PYTHONIOENCODING=utf-8
python "%~dp0rpf_audio_dump.py" --rpf "%AUDIO_RPF%" --out "%OUT_DIR%" %*

echo.
echo 完成。输出目录: %OUT_DIR%
pause
