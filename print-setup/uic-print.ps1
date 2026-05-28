# =============================================================
#  UIC 图书馆打印机一键配置 (Windows / PowerShell)
#  https://www.bnbscheduler.top/print-setup/
#  by Sirus · Contact: xiaohulimings@gmail.com
# =============================================================
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
Write-Host '  www.bnbscheduler.top  ·  by Sirus'
Write-Host '============================================'
Write-Host ''
Write-Host "目标:     $PrinterTarget  (Toshiba e-STUDIO 457)"
Write-Host "重命名为: $NewName"
Write-Host ''

# 1. Probe connectivity. Use TCP port 445 (SMB) rather than ICMP — some networks
# block ping but allow SMB, and Add-Printer needs 445 anyway.
Write-Host '[1/2] 测试与打印服务器的连通 (TCP 445)...'
$reachable = $false
try {
    $client = [System.Net.Sockets.TcpClient]::new()
    $task = $client.BeginConnect($ServerIP, 445, $null, $null)
    $reachable = $task.AsyncWaitHandle.WaitOne(2500) -and $client.Connected
    $client.Close()
} catch { $reachable = $false }

if (-not $reachable) {
    Write-Host ''
    Write-Host "  [失败] 无法连通 ${ServerIP}:445 (SMB)" -ForegroundColor Red
    Write-Host ''
    Write-Host '  必须先做下面两步再重跑此命令:'
    Write-Host '     1. 连接 UIC 校园 Wi-Fi'
    Write-Host '     2. 关闭所有代理 / VPN / 加速器'
    Write-Host ''
    Write-Host '  因为打印服务器是内网地址 (172.16.244.66), 离开校园网或开了代理'
    Write-Host '  都会让数据走不到. Add-Printer 在这种状态下会卡 30 秒以上才报错.'
    Write-Host ''
    if ($Host.Name -eq 'ConsoleHost') { Read-Host '按 Enter 关闭' }
    exit 1
}
Write-Host '   OK' -ForegroundColor Green
Write-Host ''

# 2. Add printer
Write-Host "[2/2] 正在添加打印机 $PrinterTarget ..."

function Invoke-AddPrinter {
    Add-Printer -ConnectionName $PrinterTarget -ErrorAction Stop
    Get-Printer 2>$null | Where-Object {
        $_.Name -like "*$ServerIP*" -or $_.Name -like "*\\$ServerIP\DP*"
    } | ForEach-Object {
        try { Rename-Printer -InputObject $_ -NewName $NewName -ErrorAction Stop } catch {}
    }
}

try {
    Invoke-AddPrinter
    Write-Host '   OK' -ForegroundColor Green
} catch {
    $msg = $_.Exception.Message

    # "需要提供其他用户凭据" / credential required: Windows SMB 没有 \\IP 的凭据缓存.
    # 先问用户账号密码 → cmdkey 存到凭据管理器 → 重试 Add-Printer.
    if ($msg -match '需要提供其他用户凭据|credential|Logon failure|0x8009030E|0x8007052E') {
        Write-Host '   [需要先输入账号密码] Windows 凭据管理器里没存这台服务器的账号.' -ForegroundColor Yellow
        Write-Host ''
        Write-Host '请按下面提示输入 UIC 账号 + iSpace 密码 (会保存进 Windows 凭据管理器).'
        Write-Host '账号格式: 先试单纯学号 (如 t12345678), 不行加 UIC\ 前缀 (如 UIC\t12345678)'
        Write-Host ''
        try {
            $cred = Get-Credential -Message "登录 \\$ServerIP (UIC 打印服务器)"
        } catch {
            Write-Host '   已取消.' -ForegroundColor Red
            if ($Host.Name -eq 'ConsoleHost') { Read-Host '按 Enter 关闭' }
            exit 1
        }
        $user = $cred.UserName
        $plain = $cred.GetNetworkCredential().Password
        # cmdkey 把凭据存到 Windows 凭据管理器 (跟 Mac 的钥匙串一个意思)
        $null = & cmdkey.exe /add:$ServerIP /user:$user /pass:$plain 2>$null
        Write-Host "凭据已保存. 重试添加打印机..."
        Write-Host ''
        try {
            Invoke-AddPrinter
            Write-Host '   OK' -ForegroundColor Green
            # 跳过下面的失败收尾流程
            $authRetryOK = $true
        } catch {
            $msg = $_.Exception.Message
            Write-Host ('   [仍然失败] ' + $msg) -ForegroundColor Red
            Write-Host '   可能是账号密码输错, 或者撞上了 PrintNightmare 驱动拦截 (见下方).'
            Write-Host ''
        }
    }

    if (-not $authRetryOK) {
        Write-Host ('   [失败] ' + $msg) -ForegroundColor Red
        Write-Host ''
        # 0x40 / ERROR_NETNAME_DELETED 几乎总是 PrintNightmare 补丁拦了驱动下载
        if ($msg -match '0x00000040|0x40\b|network name is no longer available|网络名称') {
            Write-Host '检测到 PrintNightmare 补丁拦截 (2021 后默认开启).' -ForegroundColor Yellow
            Write-Host 'Add-Printer 不被允许从打印服务器下载驱动. 三种解法 (推荐第一种):'
            Write-Host ''
            Write-Host '  1) 先装 Toshiba e-STUDIO 457 官方驱动 (toshibatec.com),'
            Write-Host '     装好后再跑这条命令. 本地有驱动就不去服务器下载.'
            Write-Host ''
            Write-Host '  2) 右键开始菜单 -> "终端 (管理员)", 再跑一遍这条命令.'
            Write-Host ''
            Write-Host '  3) 管理员 PowerShell 临时放开 Point and Print:'
            Write-Host "       Set-ItemProperty -Path 'HKLM:\Software\Policies\Microsoft\Windows NT\Printers\PointAndPrint' -Name 'RestrictDriverInstallationToAdministrators' -Value 0 -Type DWord"
            Write-Host '       Restart-Service Spooler'
            Write-Host '     装完务必把 Value 改回 1.'
        } else {
            Write-Host '可能原因:'
            Write-Host '  - 网络仍未通; 重连校园 Wi-Fi 后重试'
            Write-Host '  - 此电脑禁用了 SMB v1/v2 协议; 启用后重试'
            Write-Host '  - 系统太旧 (Win 7/8); 按 Win+R 输 \\172.16.244.66\DP 手动连接'
        }
        Write-Host ''
        if ($Host.Name -eq 'ConsoleHost') { Read-Host '按 Enter 关闭' }
        exit 1
    }
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
Write-Host '--------------------------------------------'
Write-Host '工具来自 www.bnbscheduler.top/print-setup'
Write-Host '作者 Sirus · 反馈 xiaohulimings@gmail.com'
Write-Host '--------------------------------------------'
Write-Host ''
if ($Host.Name -eq 'ConsoleHost') { Read-Host '按 Enter 关闭' }
