#!/bin/bash
# MAXCOURSE printing: use the reachable campus queue or open the remote portal.
# No school credentials are collected by this installer.
set -euo pipefail
PRINTER_NAME="UICPrinter"
SERVER_IP="172.16.244.66"
DEVICE_URI="smb://${SERVER_IP}/DP"
PORTAL="https://www.bnbscheduler.top/print/"
MODE="${1:-auto}"
case "$MODE" in auto|--direct|--web) ;; *) printf 'Usage: bash uic-print.command [--direct|--web]\n' >&2; exit 2 ;; esac

open_portal() {
    printf '\n打开 MAXCOURSE 网页打印：%s\n' "$PORTAL"
    printf '选择文件、预览，再填写本人学校账号和密码，提交后刷卡取件。\n'
    printf '可使用校园 Wi-Fi、手机热点或校外网络提交。\n'
    /usr/bin/open "$PORTAL"
}

printf 'MAXCOURSE 校园打印配置\n\n'
if [[ "$MODE" == '--web' ]]; then open_portal; exit 0; fi
printf '检查学校打印队列 TCP 445…\n'
if ! /usr/bin/nc -z -G 3 "$SERVER_IP" 445 >/dev/null 2>&1; then
    printf '当前网络无法直连学校打印队列。学生 Wi-Fi 与打印网络可能隔离。\n'
    if [[ "$MODE" == '--direct' ]]; then
        printf '请使用可以访问学校队列的有线网络，或打开 %s\n' "$PORTAL" >&2
        exit 1
    fi
    open_portal
    exit 0
fi

printf '队列可以连接，配置系统打印机…\n'
# Update this queue in place. Existing jobs, other printers and keychain entries stay intact.
if ! /usr/sbin/lpadmin -p "$PRINTER_NAME" -D 'UIC打印机' \
    -L '学校刷卡取件队列' -v "$DEVICE_URI" -m 'drv:///sample.drv/generic.ppd' \
    -o printer-is-shared=false -o auth-info-required=negotiate -E; then
    printf '系统打印机配置未完成。请检查打印机配置权限或驱动。\n'
    if [[ "$MODE" == '--direct' ]]; then exit 1; fi
    open_portal
    exit 0
fi
printf '\n已配置 UIC打印机。可在 App 中按 ⌘P 选择它。\n'
printf '系统要求认证时填写本人学校账号和学校密码，再到打印机刷卡取件。\n'
printf '这台系统打印机仅在当前网络能访问学校队列时可用。换到隔离的 Wi-Fi 或校外网络，请使用网页打印。\n'
printf '网页入口：%s\n' "$PORTAL"
