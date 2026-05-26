@echo off
chcp 65001 >nul
title UIC 图书馆打印机一键配置

echo ============================================
echo   UIC 图书馆打印机 一键配置
echo ============================================
echo.
echo 打印服务器  : 172.16.244.66 (sspstu.UIC.local)
echo 共享队列    : DP  (Toshiba e-STUDIO 457)
echo.
echo 使用前请确认:
echo   1. 已连接校园 Wi-Fi
echo   2. 已关闭代理 / VPN
echo   3. 用学校账号登录; 第一次打印会弹窗输账号,
echo      格式: UIC\你的学号 (例: UIC\t12345678)
echo.
echo --------------------------------------------
echo.

rem ---- 1. 探测打印服务器 ----
echo [1/2] 测试与打印服务器的连通...
ping -n 2 -w 1500 172.16.244.66 >nul
if errorlevel 1 (
    echo.
    echo [失败] 无法连通 172.16.244.66
    echo        可能原因: 没连校园 Wi-Fi, 或代理/VPN 没关
    echo.
    pause
    exit /b 1
)
echo   OK
echo.

rem ---- 2. 添加打印机 ----
echo [2/2] 正在添加打印机 \\172.16.244.66\DP ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { ^
        Add-Printer -ConnectionName '\\172.16.244.66\DP' -ErrorAction Stop; ^
        Get-Printer | Where-Object { $_.Name -like '*172.16.244.66*' -or $_.Name -like '*\\172.16.244.66\\DP*' } | ForEach-Object { Rename-Printer -InputObject $_ -NewName 'UIC打印机' -ErrorAction SilentlyContinue }; ^
        Write-Host '   OK' ^
    } catch { Write-Host ('   [失败] ' + $_.Exception.Message); exit 1 }"

if errorlevel 1 (
    echo.
    echo 添加失败。可能原因:
    echo   - 网络仍未通; 重连校园 Wi-Fi 后重试
    echo   - 此电脑禁用了 SMB v1/v2 协议; 在控制面板里启用
    echo   - 系统版本太旧; Windows 7/8 请改用 Win+R 输入:
    echo       \\172.16.244.66\DP
    echo     直接打开网络位置后右键 -- 连接
    echo.
    pause
    exit /b 1
)

echo.
echo --------------------------------------------
echo 完成! 打开 "设置 -- 蓝牙和其他设备 -- 打印机和扫描仪"
echo 应能看到一台叫 "UIC打印机" 的设备.
echo (如果显示成 "172.16.244.66 上的 DP", 自动重命名可能未生效,
echo  可以在面板里右键这台机器 -- 重命名 -- 输入 "UIC打印机".)
echo.
echo 提示: 第一次打印任何文档时,
echo   Windows 会弹窗要账号密码,
echo   账号请填    UIC\你的学号
echo   密码就是    iSpace 密码
echo   并勾选 "记住我的凭据".
echo.
echo 打印完毕后, 到图书馆任一打印机前刷学生证才会出纸
echo (PaperCut Find Me 模式).
echo.
pause
