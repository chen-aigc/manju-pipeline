# 页面文件修复脚本（需要管理员）
# 目的：把页面文件从 C 盘改到 D 盘并加大到 32GB
#      解决 ComfyUI 加载 SDXL + IP-Adapter 时的「页面文件太小」崩溃
# 用法：右键「以管理员身份运行 PowerShell」后执行本脚本，然后重启电脑

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "❌ 需要管理员权限。请右键 PowerShell -> 以管理员身份运行，再执行本脚本。" -ForegroundColor Red
    exit 1
}

Write-Host "=== 修改前状态 ===" -ForegroundColor Cyan
$cs = Get-CimInstance Win32_ComputerSystem
Write-Host "  AutomaticManagedPagefile: $($cs.AutomaticManagedPagefile)"

# 1. 关闭自动管理
Write-Host "`n[1/3] 关闭页面文件自动管理..." -ForegroundColor Yellow
$cs | Set-CimInstance -Property @{AutomaticManagedPagefile = $false}
Write-Host "  OK"

# 2. 删除 C 盘页面文件配置
Write-Host "`n[2/3] 移除 C 盘页面文件配置..." -ForegroundColor Yellow
$cPf = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue |
       Where-Object { $_.Name -like 'C:\*' }
if ($cPf) {
    $cPf | Remove-CimInstance
    Write-Host "  已移除 $($cPf.Name)"
} else {
    Write-Host "  C 盘无显式配置，跳过"
}

# 3. 在 D 盘创建 32GB 页面文件
Write-Host "`n[3/3] 在 D 盘创建 32GB 页面文件..." -ForegroundColor Yellow
$dPf = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue |
       Where-Object { $_.Name -like 'D:\*' }
if ($dPf) {
    $dPf | Set-CimInstance -Property @{InitialSize = 32768; MaximumSize = 32768}
    Write-Host "  已更新 D 盘配置为 32768 MB"
} else {
    New-CimInstance -ClassName Win32_PageFileSetting `
        -Property @{Name = 'D:\pagefile.sys'; InitialSize = 32768; MaximumSize = 32768} | Out-Null
    Write-Host "  已创建 D:\pagefile.sys 32768 MB"
}

Write-Host "`n=== 修改后状态 ===" -ForegroundColor Cyan
Get-CimInstance Win32_PageFileSetting | ForEach-Object {
    Write-Host "  $($_.Name)  初始 $($_.InitialSize) MB  最大 $($_.MaximumSize) MB"
}

Write-Host "`n✅ 配置完成。请重启电脑生效。" -ForegroundColor Green
Write-Host "   重启后 C 盘会自动释放约 17.9 GB 空间，D 盘会生成 pagefile.sys。" -ForegroundColor Gray
