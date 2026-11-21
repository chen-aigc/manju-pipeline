# 页面文件修复（管理员）—— 用 wmic 方式，避免 CIM 类型问题
$ErrorActionPreference = 'Continue'

Write-Host "=== 修改前 ===" -ForegroundColor Cyan
$cs = Get-CimInstance Win32_ComputerSystem
Write-Host "  AutomaticManagedPagefile: $($cs.AutomaticManagedPagefile)"
Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue |
    ForEach-Object { Write-Host "  $($_.Name)  $($_.InitialSize)/$($_.MaximumSize)" }

Write-Host "`n[1] 删除现有页面文件配置..." -ForegroundColor Yellow
$old = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue
if ($old) {
    foreach ($o in $old) {
        Write-Host "  删除 $($o.Name)"
        $o | Remove-CimInstance -ErrorAction SilentlyContinue
    }
}

Write-Host "`n[2] 用 wmic 创建 D:\pagefile.sys 32768 MB..." -ForegroundColor Yellow
$out = & wmic pagefileset create name="D:\pagefile.sys", InitialSize=32768, MaximumSize=32768 2>&1
Write-Host "  wmic: $out"

Write-Host "`n[3] 校验结果..." -ForegroundColor Yellow
$ok = $false
$after = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue
if ($after) {
    foreach ($a in $after) {
        Write-Host "  $($a.Name)  初始 $($a.InitialSize) MB  最大 $($a.MaximumSize) MB"
        if ($a.Name -like 'D:*' -and $a.MaximumSize -ge 32768) { $ok = $true }
    }
}

# wmic 失败时兜底：写注册表
if (-not $ok) {
    Write-Host "`n[3b] wmic 未生效，改用注册表方式..." -ForegroundColor Yellow
    $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management'
    try {
        Set-ItemProperty -Path $key -Name 'PagingFiles' -Value @('D:\pagefile.sys 32768 32768') -Type MultiString -ErrorAction Stop
        Write-Host "  已写入注册表 PagingFiles = D:\pagefile.sys 32768 32768"
        $ok = $true
    } catch {
        Write-Host "  注册表写入失败: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host "`n=== 结论 ===" -ForegroundColor Cyan
if ($ok) {
    Write-Host "✅ 配置成功，请重启电脑生效" -ForegroundColor Green
    Write-Host "   重启后：C 盘释放约 17.9 GB，D 盘生成 32 GB 页面文件" -ForegroundColor Gray
} else {
    Write-Host "❌ 配置未成功，请把上面的输出发给我" -ForegroundColor Red
}
