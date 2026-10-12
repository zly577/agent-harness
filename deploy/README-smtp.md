# 注册验证码邮件配置

用户可以使用 QQ、163、Outlook 或企业邮箱接收验证码，没有收件域名白名单。以下配置是部署者用于发送验证码的一个邮箱账号，不需要为每种收件邮箱分别配置账号。

Docker 修改仓库根目录 `.env`；直接运行 Python 后端修改 `backend/.env`。填写后重启后端；Docker 使用 `docker compose up -d --build backend`。

## 常见邮箱

| SMTP_PROVIDER | 发件服务 | 自动配置的服务器 | 加密 |
| --- | --- | --- | --- |
| qq | QQ 邮箱 | smtp.qq.com:465 | SSL |
| 163 | 网易 163 | smtp.163.com:465 | SSL |
| 126 | 网易 126 | smtp.126.com:465 | SSL |
| tencent-enterprise | 腾讯企业邮箱 | smtp.exmail.qq.com:465 | SSL |
| netease-enterprise | 网易企业邮箱（华东） | smtphz.qiye.163.com:465 | SSL |
| outlook | Outlook / Hotmail / Live | smtp-mail.outlook.com:587 | STARTTLS + OAuth2 |
| microsoft365 | Microsoft 365 企业邮箱 | smtp.office365.com:587 | STARTTLS + OAuth2 |
| custom | 其他企业邮箱或专用服务器 | 自行填写 SMTP_HOST / SMTP_PORT | 自行配置 |

预设会覆盖 `SMTP_HOST`、`SMTP_PORT`、`SMTP_SSL`、`SMTP_STARTTLS`。企业邮箱所属区域、专有云或管理员提供的地址不同时，使用 `custom` 填写实际服务器。不要根据收件人域名切换发件服务器。

QQ / 163 / 126 / 腾讯或网易企业邮箱示例：

```dotenv
ALLOW_REGISTRATION=true
SMTP_PROVIDER=qq
SMTP_USERNAME=你的发件邮箱
SMTP_FROM=你的发件邮箱
SMTP_PASSWORD=邮箱服务生成的客户端授权码
SMTP_AUTH_MODE=auto
```

先在邮箱设置中开启 SMTP/客户端访问并生成授权码，具体权限由邮箱管理员决定。不要把邮箱网页登录密码当作授权码。`SMTP_FROM` 必须是账号有权发送的地址。

## 微软邮箱

微软预设默认使用 OAuth2，不会在 OAuth 失败后回退到密码认证。需要先在 Microsoft Entra 注册自己的应用；这一步需要账号持有人/企业管理员授权，项目不能代替你生成真实的账号凭据。

Outlook 个人邮箱或委托授权的企业邮箱：

```dotenv
SMTP_PROVIDER=outlook
SMTP_USERNAME=你的发件邮箱
SMTP_FROM=你的发件邮箱
SMTP_OAUTH_TENANT=common
SMTP_OAUTH_CLIENT_ID=应用客户端ID
SMTP_OAUTH_REFRESH_TOKEN=已授权的刷新令牌
# 机密客户端还需填写客户端密钥，公共客户端可留空
SMTP_OAUTH_CLIENT_SECRET=
```

委托授权需请求 `https://outlook.office.com/SMTP.Send` 和 `offline_access`。发送时自动用 refresh token 换取 access token，无需手动填写短期 access token。刷新令牌被撤销或过期时需重新授权。

Microsoft 365 无人值守企业应用也支持 client credentials：

```dotenv
SMTP_PROVIDER=microsoft365
SMTP_USERNAME=被授权的企业发件邮箱
SMTP_FROM=被授权的企业发件邮箱
SMTP_OAUTH_TENANT=实际租户ID
SMTP_OAUTH_CLIENT_ID=应用客户端ID
SMTP_OAUTH_CLIENT_SECRET=应用密钥
SMTP_OAUTH_REFRESH_TOKEN=
```

管理员需授予 Exchange Online 的 `SMTP.SendAsApp` 应用权限、完成管理员同意，在 Exchange 注册服务主体并授权指定邮箱，同时确认该邮箱允许 SMTP AUTH。不要只创建一个应用密钥就认为已经获得邮箱发信权限。中国区或其他主权云的 OAuth 端点未集成，当前微软 OAuth 适配面向全球云。

## 自定义企业 SMTP

```dotenv
SMTP_PROVIDER=custom
SMTP_HOST=管理员提供的SMTP服务器
SMTP_PORT=587
SMTP_SSL=false
SMTP_STARTTLS=true
SMTP_USERNAME=发件账号
SMTP_PASSWORD=授权码或管理员提供的凭据
SMTP_FROM=发件邮箱
```

使用 465 端口时设置 `SMTP_SSL=true`。账号认证必须使用 TLS，不支持把密码明文发送到未加密的服务器。仅无账号的内部测试/中继服务器可以关闭 TLS。

## 验证配置

Docker（读取容器实际使用的配置）：

```sh
docker compose exec backend python -m scripts.check_smtp
docker compose exec backend python -m scripts.check_smtp --to 你的测试收件邮箱
```

本地开发，在 `backend` 目录运行：

```sh
python -m scripts.check_smtp
python -m scripts.check_smtp --to 你的测试收件邮箱
```

第一条只验证连接和认证；第二条才发送测试邮件。服务器接受邮件不等于已送达收件箱，还需检查收件箱/垃圾邮件。真实授权码、应用密钥与刷新令牌仅保存在本机配置或部署密钥管理中，不提交到 GitHub。CI 使用本地测试 SMTP，不需要真实邮箱账号。

参考：[腾讯官方常见 SMTP 配置](https://intl.cloud.tencent.com/document/product/1266/71700)、[Outlook 官方配置](https://support.microsoft.com/en-au/outlook/pop-imap-and-smtp-settings-for-outlook-com)、[微软 SMTP OAuth 授权说明](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)。
