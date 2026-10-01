# MAXCOURSE printing: use the reachable campus queue or open the remote portal.
# Passwords are entered in Windows or on the HTTPS portal, never in this installer.
param([ValidateSet('Auto', 'Direct', 'Web')][string]$Mode = 'Auto')
$ErrorActionPreference = 'Stop'
$ServerIP = '172.16.244.66'
$PrinterTarget = '\\172.16.244.66\DP'
$PrinterName = 'UIC打印机'
$Portal = 'https://www.bnbscheduler.top/print/'

function Open-PrintPortal {
    Write-Host "打开 MAXCOURSE 网页打印：$Portal"
    Write-Host '选择文件、预览，再填写本人学校账号和密码，提交后刷卡取件。'
    Write-Host '可使用校园 Wi-Fi、手机热点或校外网络提交。'
    Start-Process $Portal
}

Write-Host 'MAXCOURSE 校园打印配置'
if ($Mode -eq 'Web') { Open-PrintPortal; return }
Write-Host '检查学校打印队列 TCP 445…'
$client = $null
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $pending = $client.BeginConnect($ServerIP, 445, $null, $null)
    $reachable = $pending.AsyncWaitHandle.WaitOne(3000) -and $client.Connected
    if ($reachable) { $client.EndConnect($pending) }
} catch { $reachable = $false } finally { if ($client) { $client.Close() } }
if (-not $reachable) {
    Write-Host '当前网络无法直连学校打印队列。学生 Wi-Fi 与打印网络可能隔离。'
    if ($Mode -eq 'Direct') { throw "请使用可访问学校队列的有线网络，或打开 $Portal" }
    Open-PrintPortal
    return
}

try {
    $existing = Get-Printer -ErrorAction Stop | Where-Object {
        $_.Name -eq $PrinterTarget -or ($_.Name -eq $PrinterName -and ($_.PortName -eq $PrinterTarget -or $_.PortName -eq $ServerIP))
    } | Select-Object -First 1
    if (-not $existing) {
        # Use a shared connection with its signed school driver. Do not weaken driver policies.
        Add-Printer -ConnectionName $PrinterTarget -ErrorAction Stop
    }
    Write-Host '学校共享打印机已连接，可在 App 中按 Ctrl+P 选择 DP 队列。'
    Write-Host '系统要求认证时填写 UIC\学号和学校密码，再到打印机刷卡取件。'
    Write-Host '换到隔离的 Wi-Fi 或校外网络时，请使用网页打印。'
} catch {
    Write-Host ('系统打印机配置未完成：' + $_.Exception.Message)
    Write-Host '如 Windows 要求安装驱动，请使用学校提供的签名驱动，或使用网页打印。'
    if ($Mode -eq 'Direct') { throw }
    Open-PrintPortal
}
