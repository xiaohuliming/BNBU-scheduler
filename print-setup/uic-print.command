#!/bin/bash
# =============================================================
#  UIC 图书馆打印机一键配置 (macOS)
#  https://www.bnbscheduler.top/print-setup/
#  by Sirus · Contact: xiaohulimings@gmail.com
# =============================================================
#
# 这个脚本调用 macOS 自带的 lpadmin 添加 UIC 图书馆共享 SMB 打印机.
# - 不修改任何系统级配置 (CUPS 用户级队列)
# - 不需要 sudo / 管理员密码
# - 可以随时卸载: lpadmin -x UICPrinter

set -e

PRINTER_NAME="UICPrinter"
DEVICE_URI="smb://172.16.244.66/DP"
DESCRIPTION="UIC打印机"
LOCATION="UIC 图书馆 (PaperCut · Find Me)"
PPD="drv:///sample.drv/generic.ppd"

clear
echo "============================================"
echo "  UIC 图书馆打印机配置 (macOS)"
echo "  www.bnbscheduler.top  ·  by Sirus"
echo "============================================"
echo ""
echo "目标:     ${DESCRIPTION}"
echo "地址:     ${DEVICE_URI}"
echo "驱动:     Generic PostScript Printer"
echo ""

# 1. 测试连通 (仅警告, 不阻断)
echo "[1/2] 测试与打印服务器的连通 (1.5s)..."
if /sbin/ping -c 1 -t 2 172.16.244.66 >/dev/null 2>&1; then
    echo "   OK"
else
    echo "   警告: ping 不通 172.16.244.66"
    echo "         可能没连校园 Wi-Fi 或开了代理 / VPN."
    echo "         打印机仍会被添加, 但实际打印时会失败."
fi
echo ""

# 2. 已存在则先移除 (含旧版命名, idempotent)
for OLD in "${PRINTER_NAME}" "UIC_图书馆打印" "UIC_Library_Print"; do
    if lpstat -p "${OLD}" >/dev/null 2>&1; then
        echo "检测到已存在的 ${OLD}, 先移除..."
        lpadmin -x "${OLD}" 2>/dev/null || true
    fi
done

# 3. 添加打印机
echo "[2/2] 正在添加打印机..."
if lpadmin -p "${PRINTER_NAME}" \
    -L "${LOCATION}" \
    -D "${DESCRIPTION}" \
    -v "${DEVICE_URI}" \
    -m "${PPD}" \
    -o printer-is-shared=false \
    -E
then
    # 关键: 用 negotiate (跟系统设置 GUI 手动添加时一样). 这个值让 SMB 后端
    # 做交互式认证, 提交打印就"立即弹"登录框. 而 username,password 是 CUPS
    # 层认证, 任务会挂起等你手动点刷新补凭据 (体验差, 已弃用).
    lpadmin -p "${PRINTER_NAME}" -o auth-info-required=negotiate 2>/dev/null || true
    cupsenable "${PRINTER_NAME}" 2>/dev/null || true
    cupsaccept "${PRINTER_NAME}" 2>/dev/null || true
    echo "   OK (已设置: 打印时会自动弹账号密码框)"
else
    echo ""
    echo "添加失败. 试着检查:"
    echo "  - 你的账户是否在 lpadmin 组里 (绝大多数 Mac 都是)"
    echo "  - PPD 路径是否变了 (打 'ls /System/Library/Frameworks/ApplicationServices.framework/Versions/A/Frameworks/PrintCore.framework/Resources/' 看看)"
    echo ""
    echo "(按 Return 关闭)"
    read
    exit 1
fi

echo ""
echo "--------------------------------------------"
echo "完成! 打开 '系统设置 -> 打印机与扫描仪'"
echo "应能看到一台叫 '${DESCRIPTION}' 的设备."
echo ""
echo "第一次打印:"
echo "  1. 任意 App 按 Cmd+P, 选这台打印机, 点打印"
echo "  2. 系统弹账号窗口:"
echo "       账号: 先试单纯学号 (如 t12345678),"
echo "             不行改成 UIC\\t12345678"
echo "       密码: iSpace 密码"
echo "       勾'记住此密码'"
echo "  3. 走到图书馆任意 Toshiba e-STUDIO 前刷学生证 release"
echo ""
echo "想要卸载这台打印机, 在终端跑:"
echo "   lpadmin -x ${PRINTER_NAME}"
echo "(或者直接在'系统设置 -> 打印机与扫描仪'里删除)"
echo ""
echo "--------------------------------------------"
echo "工具来自 www.bnbscheduler.top/print-setup"
echo "作者 Sirus · 反馈 xiaohulimings@gmail.com"
echo "--------------------------------------------"
echo ""
echo "(按 Return 关闭此窗口)"
read
