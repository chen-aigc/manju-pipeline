@echo off
chcp 65001 >nul
title 修复页面文件（需要管理员）
echo.
echo ========================================
echo   页面文件修复工具
echo   目的：把页面文件从 C 盘改到 D 盘并加到 32GB
echo   解决 ComfyUI 加载 SDXL + IP-Adapter 时的崩溃
echo ========================================
echo.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [错误] 未以管理员身份运行！
    echo.
    echo 请右键点击本文件 -^> 以管理员身份运行
    echo.
    pause
    exit /b 1
)

echo [当前状态]
powershell -NoProfile -Command "Get-CimInstance Win32_ComputerSystem | ForEach-Object { Write-Host ('  AutomaticManagedPagefile: ' + $_.AutomaticManagedPagefile) }"
echo.

echo [1/3] 关闭自动管理...
powershell -NoProfile -Command "$cs = Get-CimInstance Win32_ComputerSystem; $cs | Set-CimInstance -Property @{AutomaticManagedPagefile=[bool]$false}"

echo [2/3] 移除 C 盘页面文件配置...
powershell -NoProfile -Command "Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'C:*' } | Remove-CimInstance -ErrorAction SilentlyContinue"

echo [3/3] 在 D 盘创建 32GB 页面文件...
powershell -NoProfile -Command "$d = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'D:*' }; if ($d) { $d | Set-CimInstance -Property @{InitialSize=32768; MaximumSize=32768} } else { New-CimInstance -ClassName Win32_PageFileSetting -Property @{Name='D:\pagefile.sys'; InitialSize=32768; MaximumSize=32768} | Out-Null }"

echo.
echo [修改后状态]
powershell -NoProfile -Command "Get-CimInstance Win32_PageFileSetting | ForEach-Object { Write-Host ('  ' + $_.Name + '  初始 ' + $_.InitialSize + ' MB  最大 ' + $_.MaximumSize + ' MB') }"
echo.
echo ========================================
echo   完成！请重启电脑生效。
echo   重启后 C 盘会释放约 17.9 GB
echo ========================================
echo.
pause
