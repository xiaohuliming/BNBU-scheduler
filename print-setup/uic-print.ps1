# UIC 图书馆打印机一键配置 (Windows / PowerShell)
#
# 用法 (在 PowerShell 里运行):
#   irm https://www.bnbscheduler.top/print-setup/uic-print.ps1 | iex
#
# 这个脚本调用 Windows 自带的 Add-Printer + Rename-Printer.
# 不需要管理员权限; Add-Printer -ConnectionName 走用户级队列.

$ErrorActionPreference = 'Stop'
try { $Host.UI.RawUI.WindowTitle = 'UIC 图书馆打印机配置' } catch {}

$PrinterTarget = '\\172.16.244.66\DP'
$ServerIP = '172.16.244.66'
$NewName = 'UIC打印机'

Write-Host '============================================'
Write-Host '  UIC 图书馆打印机 一键配置 (PowerShell)'
Write-Host '============================================'
Write-Host ''
Write-Host "目标:     $PrinterTarget  (Toshiba e-STUDIO 457)"
Write-Host "重命名为: $NewName"
Write-Host ''

# 1. Probe connectivity
Write-Host '[1/2] 测试与打印服务器的连通...'
$reachable = $false
try {
    $reachable = Test-Connection -ComputerName $ServerIP -Count 1 -Quiet -ErrorAction Stop
} catch { $reachable = $false }

if (-not $reachable) {
    Write-Host ''
    Write-Host "  [失败] 无法 ping 通 $ServerIP" -ForegroundColor Red
    Write-Host '         没连校园 Wi-Fi, 或代理 / VPN 没关.'
    Write-Host '         打印机仍会被添加, 但实际打印时会失败.'
    Write-Host ''
} else {
    Write-Host '   OK' -ForegroundColor Green
    Write-Host ''
}

# 2. Add printer
Write-Host "[2/2] 正在添加打印机 $PrinterTarget ..."
try {
    Add-Printer -ConnectionName $PrinterTarget -ErrorAction Stop
    # 自动重命名 (新版 Windows 上对 connection printer 有时不支持, 失败时静默忽略)
    Get-Printer 2>$null | Where-Object {
        $_.Name -like "*$ServerIP*" -or $_.Name -like "*\\$ServerIP\DP*"
    } | ForEach-Object {
        try { Rename-Printer -InputObject $_ -NewName $NewName -ErrorAction Stop } catch {}
    }
    Write-Host '   OK' -ForegroundColor Green
} catch {
    Write-Host ('   [失败] ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host ''
    Write-Host '可能原因:'
    Write-Host '  - 网络仍未通; 重连校园 Wi-Fi 后重试'
    Write-Host '  - 此电脑禁用了 SMB v1/v2 协议; 启用后重试'
    Write-Host '  - 系统太旧 (Win 7/8); 按 Win+R 输 \\172.16.244.66\DP 手动连接'
    Write-Host ''
    if ($Host.Name -eq 'ConsoleHost') { Read-Host '按 Enter 关闭' }
    exit 1
}

Write-Host ''
Write-Host '--------------------------------------------'
Write-Host "完成! 打开'设置 -> 蓝牙和其他设备 -> 打印机和扫描仪'"
Write-Host "应能看到一台叫 '$NewName' 的设备."
Write-Host "(若仍显示为 '172.16.244.66 上的 DP', 在面板里右键 -> 重命名 -> 输入 '$NewName')"
Write-Host ''
Write-Host '第一次打印 (必须在校园网内发起):'
Write-Host '  - 任意 App 按 Ctrl+P, 选这台打印机'
Write-Host '  - Windows 弹凭据窗口:'
Write-Host '      用户名: 先输学号 (如 t12345678), 不行加 UIC\ 前缀 (UIC\t12345678)'
Write-Host '      密码:   iSpace 密码'
Write-Host '      勾选 "记住我的凭据"'
Write-Host '  - 打印后到图书馆任一 Toshiba 打印机前刷学生证 release'
Write-Host ''
Write-Host '想卸载这台打印机:'
Write-Host "  Remove-Printer -Name '$NewName'"
Write-Host ''
if ($Host.Name -eq 'ConsoleHost') { Read-Host '按 Enter 关闭' }
