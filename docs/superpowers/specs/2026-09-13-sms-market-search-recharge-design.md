# SMS Market 关联词搜索与在线充值设计

日期：2026-09-13

## 目标

1. 服务搜索支持常见品牌别名、产品名和中文名称。例如搜索 `ChatGPT`、`GPT` 或 `OpenAI` 都能找到 HeroSMS 的 OpenAI 服务。
2. SMS Market 增加在线充值，复用 OmniChat 已上线的 XorPay 支付宝收银台和验签回调。
3. 充值成功只增加 SMS Market 的 USD 钱包，不增加 OmniChat 积分。
4. 固定汇率为 `1 USD = 6.80 CNY`，下单时锁定人民币金额和美元到账金额。

## 非目标

- 不合并 OmniChat 积分与 SMS Market USD 钱包。
- 不在 MAXCOURSE 中复制或暴露 XorPay 商户密钥。
- 不开放任意金额或可由前端修改的汇率。
- 不改变现有 HeroSMS 购买加价 50% 的规则。

## 关联词搜索

服务目录由 MAXCOURSE 后端继续从 HeroSMS 获取。后端为已知服务代码附加经过人工维护的 `aliases` 数组，前端搜索时同时匹配服务名称、服务代码、英文品牌别名、常用产品名和中文俗称。

首批至少覆盖常用服务。OpenAI 服务代码 `dr` 的别名包括 `chatgpt`、`gpt`、`gpt-4`、`open ai` 和 `人工智能`。别名只用于检索，不改变页面显示的服务名称、Logo、报价或购买时提交的服务代码。

搜索字符串统一转为小写，并移除空格、连字符和常见标点后再匹配，因此 `Chat GPT`、`chat-gpt` 和 `chatgpt` 的结果一致。无匹配时继续显示当前空状态。

## 充值产品与计价

只提供三个固定套餐：

| SMS 钱包到账 | 支付金额 |
| --- | --- |
| 1 USD | 6.80 CNY，即 680 分 |
| 5 USD | 34.00 CNY，即 3400 分 |
| 10 USD | 68.00 CNY，即 6800 分 |

所有金额使用整数存储：人民币使用分，SMS 钱包使用万分之一美元。浏览器只提交套餐标识，OmniChat 服务端根据受信配置计算人民币金额和到账单位。订单创建后，即使以后调整汇率，该订单仍按创建时保存的金额结算。

## 系统边界

OmniChat 继续作为支付所有者，负责保存支付订单、使用现有 XorPay 商户配置签名收银台地址、验证异步回调、校验商户、订单、金额和第三方交易号，以及幂等地把订单更新为已支付。

MAXCOURSE 继续作为 SMS 钱包所有者，负责展示充值套餐和订单状态、通过当前跨站 SSO 身份请求 OmniChat 创建订单、打开受信收银台入口、查询当前共享账号的支付结果，以及把已支付订单幂等结算到当前用户的 SMS 钱包。

XorPay 密钥只保留在 OmniChat 私有配置中。MAXCOURSE 不读取、不保存、不转发商户密钥。

## OmniChat 接口扩展

新增 SMS Market 专用接口，继续使用现有 `auth.current_user` 身份校验：

- `GET /api/recharge/sms-market/config`
- `POST /api/recharge/sms-market/orders`
- `GET /api/recharge/sms-market/orders`
- `GET /api/recharge/sms-market/orders/{order_id}`
- `GET /api/recharge/sms-market/orders/{order_id}/checkout`

创建接口接收套餐标识和 `request_id`，创建 `app = 'sms_market'` 的订单。同一账号、同一应用、同一 `request_id` 只能对应一笔订单。查询接口只返回当前共享账号拥有的 SMS Market 订单。收银台根据数据库中的受信金额重建，并固定返回 SMS Market 页面。

现有 XorPay 回调继续由 OmniChat 接收。`app = 'omnichat'` 的订单沿用当前积分入账逻辑。`app = 'sms_market'` 的订单只标记支付成功，不写入 OmniChat 积分。

数据库中的请求幂等约束按 `user_id + app + request_id` 生效。待支付订单数量也按应用分别限制，防止 SMS Market 订单占满 OmniChat 充值额度。

## MAXCOURSE 接口扩展

新增同源代理接口：

- `GET /api/sms-lab/recharge/config`
- `POST /api/sms-lab/recharge/orders`
- `GET /api/sms-lab/recharge/orders`
- `GET /api/sms-lab/recharge/orders/{order_id}`

MAXCOURSE 使用浏览器已有的父域 `sso_token` 向 OmniChat 请求，不接受前端提交用户名或目标用户 ID。没有有效共享身份时要求用户重新登录，不能退化为按用户名猜测账户。

MAXCOURSE 观察到当前账号的 SMS Market 订单已支付时，在一个本地 SQLite 事务中使用 `online_recharge:{order_id}` 作为 SMS 钱包流水唯一引用。只有首次写入该引用时才增加 `sms_wallet_units`。重复轮询、页面刷新、支付回调重放和并发请求都返回同一余额，不会重复入账。

已支付订单保存在 OmniChat 共享账本中，所以 MAXCOURSE 临时不可用时不会丢失充值。用户下次打开 SMS Market 或查看订单时会自动补结算。

## 前端交互

- 钱包金额旁新增“充值”按钮。
- 余额不足提示中的“充值”可直接打开充值弹窗。
- 弹窗展示 1、5、10 USD 三个手绘卡片，同时显示对应人民币实付金额。
- 用户确认后打开 XorPay 支付宝收银台。
- 返回 SMS Market 后自动恢复订单并轮询状态。
- 支付成功显示到账金额和最新 USD 余额。
- 待支付、已到账、已关闭和异常状态使用明确文案。
- 创建请求期间禁用重复提交，同一未确认请求复用原订单。
- 保持当前卡通手绘视觉、移动端 44px 触控目标和键盘可访问性。

## 安全与故障处理

- 前端金额、用户名、用户 ID、回调状态均不可信。
- XorPay 回调必须通过现有 MD5 签名、商户号、支付状态、精确金额和交易号校验。
- 同一第三方交易号只能结算一笔订单。
- 收银台金额从数据库订单重建，不能从 URL 查询参数覆盖。
- 支付浏览器返回只触发查询，不直接触发入账。
- OmniChat 不可用时保留 SMS 当前余额并显示“充值服务暂时不可用”。
- MAXCOURSE 不可用时，OmniChat 仍保存已支付状态，恢复后可补结算。
- 日志不记录 SSO token、XorPay 密钥、完整回调查询串或用户密码。

## 验证范围

- `chatgpt`、`Chat GPT`、`open-ai` 均命中 OpenAI 服务。
- 无关词不会错误命中。
- 三个套餐人民币金额精确为 680、3400、6800 分。
- 篡改套餐金额、用户 ID 或订单归属会被拒绝。
- 同一 `request_id` 并发创建只产生一笔订单。
- SMS Market 订单支付后不增加 OmniChat 积分。
- 合法支付只增加一次 SMS 钱包。
- 回调重放、重复轮询和并发结算不会重复增加钱包。
- 错误签名、错误金额、错误商户和重复第三方交易号不能入账。
- OmniChat 暂时不可用和 MAXCOURSE 延迟结算均可恢复。
- 桌面端与移动端完成登录、选套餐、跳转支付、返回、到账的浏览器流程。

发布时先部署兼容旧接口的 OmniChat，再部署 MAXCOURSE。线上验收需要检查两个服务的有效版本、支付配置公开字段、模拟回调测试和一笔受控的小额真实支付。
