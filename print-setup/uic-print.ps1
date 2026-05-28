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

function Find-BestDriver {
    # 优先用 Toshiba 官方驱动 (画质最好); 其次系统自带 PS 通用驱动 (够用且总有).
    # 实在没有再 fallback 到 -ConnectionName (会触发 PrintNightmare 那条坑路).
    $installed = @(Get-PrinterDriver -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
    $preferred = @(
        'TOSHIBA Universal Printer 2',
        'TOSHIBA Universal Printer',
        'TOSHIBA e-STUDIO PS3',
        'TOSHIBA e-STUDIO PCL6 v4',
        'TOSHIBA e-STUDIO PCL6',
        'Microsoft PS Class Driver',
        'Generic / Text Only'
    )
    foreach ($name in $preferred) {
        if ($installed -contains $name) { return $name }
    }
    $tosh = $installed | Where-Object { $_ -like '*TOSHIBA*' } | Select-Object -First 1
    if ($tosh) { return $tosh }
    $ps = $installed | Where-Object { $_ -like '*PS Class*' -or $_ -like '*PostScript*' } | Select-Object -First 1
    if ($ps) { return $ps }
    return $null
}

function Invoke-AddPrinter {
    # 先清掉之前失败留下的同名 / 同地址打印机 (让重跑脚本可以幂等执行).
    $stale = Get-Printer -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -eq $NewName -or $_.PortName -like "*$ServerIP*" -or $_.Name -like "*$ServerIP*"
    }
    foreach ($p in $stale) {
        try {
            Remove-Printer -Name $p.Name -ErrorAction Stop
            Write-Host "   清理旧打印机: $($p.Name)" -ForegroundColor DarkGray
        } catch {}
    }

    $driver = Find-BestDriver
    if ($driver) {
        Write-Host "   使用本地驱动: $driver" -ForegroundColor Cyan
        # 先确保 UNC 端口存在 (不影响已存在的)
        try { Add-PrinterPort -Name $PrinterTarget -ErrorAction Stop } catch {
            # PrinterPort 已存在会抛, 忽略即可
            if ($_.Exception.Message -notmatch '已存在|exists') { throw }
        }
        # 用本地驱动 + UNC 端口添加打印机 — 完全不需要从服务器下载驱动,
        # 绕开 PrintNightmare 整套拦截.
        Add-Printer -Name $NewName -DriverName $driver -PortName $PrinterTarget -ErrorAction Stop
    } else {
        # 实在没找到任何可用驱动 -> 退回到 -ConnectionName, 让服务器送驱动
        # (大概率撞 PrintNightmare, 但已经没有其他办法)
        Write-Host '   本机没找到可用驱动, 尝试从服务器下载 (可能被 PrintNightmare 拦)...' -ForegroundColor Yellow
        Add-Printer -ConnectionName $PrinterTarget -ErrorAction Stop
        # ConnectionName 模式下打印机名是固定的, 试着 rename 一下
        Get-Printer 2>$null | Where-Object {
            $_.Name -like "*$ServerIP*" -or $_.Name -like "*\\$ServerIP\DP*"
        } | ForEach-Object {
            try { Rename-Printer -InputObject $_ -NewName $NewName -ErrorAction Stop } catch {}
        }
    }
}

function Format-PrinterError {
    param($err)
    $parts = @()
    if ($err.Exception.Message) { $parts += $err.Exception.Message }
    if ($err.ErrorDetails -and $err.ErrorDetails.Message) {
        $parts += "[Details] " + $err.ErrorDetails.Message
    }
    $inner = $err.Exception.InnerException
    while ($inner) {
        if ($inner.Message) { $parts += "[Inner] " + $inner.Message }
        $inner = $inner.InnerException
    }
    if ($err.Exception.HResult) {
        $hex = "0x{0:X8}" -f $err.Exception.HResult
        $parts += "[HResult] $hex"
    }
    return ($parts -join " | ")
}

try {
    Invoke-AddPrinter
    Write-Host '   OK' -ForegroundColor Green
} catch {
    $msg = Format-PrinterError $_

    # "需要提供其他用户凭据" / credential required: Windows SMB 没有 \\IP 的凭据缓存.
    # 先问用户账号密码 → cmdkey 存到凭据管理器 → 重试 Add-Printer.
    if ($msg -match '需要提供其他用户凭据|credential|Logon failure|0x8009030E|0x8007052E') {
        Write-Host '   [需要先输入账号密码] Windows 凭据管理器里没存这台服务器的账号.' -ForegroundColor Yellow
        Write-Host ''
        Write-Host '请按下面提示输入 UIC 账号 + iSpace 密码 (会保存进 Windows 凭据管理器).'
        Write-Host ''
        Write-Host '  ** 用户名一定要带 UIC\ 前缀 **' -ForegroundColor Yellow
        Write-Host '     格式: UIC\你的学号  (例如  UIC\t12345678)'
        Write-Host '     Windows 不会自动识别 UIC 域, 不加前缀会直接被拒.'
        Write-Host '     密码就是 iSpace 密码.'
        Write-Host ''
        try {
            $cred = Get-Credential -Message "登录 \\$ServerIP — 用户名必须填 UIC\学号"
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
        Write-Host '   [失败]' -ForegroundColor Red
        Write-Host ('   ' + $msg)
        Write-Host ''

        # 把通用错误也归到 PrintNightmare 嫌疑里 —— Add-Printer 在驱动下载被拦时
        # 经常吐 "An error occurred while performing the specified operation"
        # 或 "0x00000040 / network name is no longer available".
        $isPrintNightmareLikely = $msg -match '0x00000040|0x40\b|network name is no longer available|网络名称|An error occurred while performing|执行指定的操作时发生错误'

        if ($isPrintNightmareLikely) {
            Write-Host '高度怀疑被 PrintNightmare 补丁拦了驱动下载 (2021 后默认开启).' -ForegroundColor Yellow
            Write-Host 'Add-Printer 不被允许从打印服务器自动下载驱动 → 报通用错误退出.'
            Write-Host ''
            Write-Host '按下面顺序试, 任意一种成功就停:'
            Write-Host ''
            Write-Host '  1) [最干净] 先装 Toshiba e-STUDIO 457 官方驱动:'
            Write-Host '     https://www.toshibatec.com/support_download/'
            Write-Host '     装好后再跑这条命令 (本地有驱动就不去服务器下载, 绕过整套拦截).'
            Write-Host ''
            Write-Host '  2) [试试看] 重启打印服务再跑一次 (管理员 PowerShell):'
            Write-Host '       Restart-Service Spooler'
            Write-Host '       irm https://www.bnbscheduler.top/print-setup/uic-print.ps1 | iex'
            Write-Host ''
            Write-Host '  3) [快但有安全代价] 临时放开 Point and Print 限制 (管理员 PowerShell),'
            Write-Host '     装完务必把 Value 改回 1:'
            Write-Host "       Set-ItemProperty -Path 'HKLM:\Software\Policies\Microsoft\Windows NT\Printers\PointAndPrint' -Name 'RestrictDriverInstallationToAdministrators' -Value 0 -Type DWord"
            Write-Host '       Restart-Service Spooler'
            Write-Host ''
            Write-Host '  4) [兜底] 用 Win+R 输 \\172.16.244.66, 在弹的窗口里双击 DP 那台打印机,'
            Write-Host '     Windows 会用图形界面装它 (有时图形界面能过, 命令行不能过).'
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

# --- 3. 缓存 SMB 凭据 (避免第一次打印时静默 Error) ---
# 用 -DriverName + -PortName 路径添加的打印机, Add-Printer 不联服务器, 所以 SMB
# 凭据没被缓存. 第一次打印时 spooler 找不到现成凭据 -> 任务直接标 Error 也不弹框.
# 提前用 cmdkey 缓存一份, 第一次打印就能直接走通.
Write-Host ''
Write-Host '[3/3] 缓存打印凭据 (避免第一次打印时静默失败)...'
$existing = & cmdkey.exe /list 2>$null | Select-String "Target:.*$ServerIP"
if ($existing) {
    Write-Host "   已检测到 $ServerIP 的缓存凭据 (跳过)." -ForegroundColor DarkGray
    Write-Host '   如果之前缓存的账号错了, 手动运行: cmdkey /delete:172.16.244.66'
} else {
    Write-Host ''
    Write-Host '  ** 用户名必须带 UIC\ 前缀 **' -ForegroundColor Yellow
    Write-Host '     格式: UIC\你的学号  (例如  UIC\t12345678)'
    Write-Host '     密码就是 iSpace 密码.'
    Write-Host ''
    try {
        $cred = Get-Credential -Message "缓存 \\$ServerIP 的打印凭据 — 用户名一定要 UIC\学号"
        $u = $cred.UserName
        $p = $cred.GetNetworkCredential().Password
        $null = & cmdkey.exe /add:$ServerIP /user:$u /pass:$p 2>$null
        Write-Host "   OK - 凭据已缓存 ($u)" -ForegroundColor Green
    } catch {
        Write-Host '   跳过. 第一次打印时如果任务状态显示 Error, 回头自己跑:' -ForegroundColor DarkGray
        Write-Host "       cmdkey /add:$ServerIP /user:UIC\你的学号 /pass:iSpace密码" -ForegroundColor DarkGray
    }
}

Write-Host ''
Write-Host '--------------------------------------------'
Write-Host "完成! 打开'设置 -> 蓝牙和其他设备 -> 打印机和扫描仪'"
Write-Host "应能看到一台叫 '$NewName' 的设备."
Write-Host "(若仍显示为 '172.16.244.66 上的 DP', 在面板里右键 -> 重命名 -> 输入 '$NewName')"
Write-Host ''
Write-Host '第一次打印 (必须在校园网内发起):'
Write-Host '  - 任意 App 按 Ctrl+P, 选这台打印机, 提交即可'
Write-Host '  - 如果队列里任务显示 "Error":'
Write-Host '      → 凭据没缓存或缓存错了. 跑: cmdkey /add:172.16.244.66 /user:UIC\你的学号 /pass:密码'
Write-Host '      → 然后取消那个 Error 任务, 重新打印'
Write-Host '  - 等任务传完 (进度条跑完), 到图书馆任一 Toshiba 打印机前刷学生证 release'
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
