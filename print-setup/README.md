# 校园打印配置入口

网页打印位于 https://www.bnbscheduler.top/print/ 。配置页面位于 `/print-setup/index.html`，显示网页打印的实时状态与当前文件、页数、份数上限。

安装脚本先检测学校共享队列 `172.16.244.66:445`。当前网络可以访问时，Mac 配置 `UICPrinter`，Windows 连接学校 `DP` 共享队列。检测失败或安装失败时，默认打开网页打印。网页打印适用于校园 Wi-Fi、手机热点及校外网络。

网页地址不是系统打印协议地址。当前脚本不会把网页地址假装成 IPP 或 SMB 打印机。需要系统打印窗口直接提交的用户，仍需使用能访问学校队列的网络。

Mac 可显式使用 `--web` 或 `--direct`，PowerShell 可使用 `-Mode Web` 或 `-Mode Direct`。Direct 模式的连接或安装失败会明确报错。脚本不读取学校密码、不取消既有打印任务，也不删除钥匙串条目。Windows 安装学校驱动时按系统权限要求处理，脚本不修改驱动安装策略。

发布时同步检查 `uic-print.command`、`uic-print.ps1`、`uic-print.bat` 与 `uic-print-mac.zip`。ZIP 必须包含当前 Mac 脚本并保留可执行权限。BAT 保持 GBK 与 CRLF，调用线上 UTF-8 PowerShell 脚本，避免长期保留另一份安装逻辑。旧的 mobileconfig 只适用于队列可达的网络，说明包含网页替代入口。

页面使用本地字体与预编译 Tailwind，不依赖海外 CDN。修改 Tailwind 类后，在仓库根目录执行以下命令，输入文件只需三条 `@tailwind` 指令：

```bash
npx --yes tailwindcss@3.4.17 -i /path/to/tailwind-input.css -o print-setup/tailwind.static.css --content print-setup/index.html --minify
```

2026-10-01 验证：Mac 脚本的连接成功、连接失败、显式模式、安装失败与 ZIP 一致性共 7 项测试通过，包含页面复制命令在默认 zsh 中执行的检查。PowerShell 7.6.6 在隔离的本地 TCP 服务上验证了成功和失败分支，打印机和浏览器命令使用记录替代，没有修改真实系统打印机。该检查验证脚本流程，不代替 Windows 机器上的共享驱动安装与实物打印验收。浏览器验证在线和离线状态、操作系统切换、下载地址、手动配置切换及手机布局通过，未发起真实打印任务。
