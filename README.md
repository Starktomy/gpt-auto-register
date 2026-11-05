<h1 align="center">gpt-outlook-register</h1>

<p align="center">纯协议 ChatGPT 账号注册机：<code>curl_cffi</code> 模拟 TLS 指纹 + QuickJS 解 OpenAI Sentinel PoW + 邮箱取 OTP，直接走 OpenAI authorize 状态机，无浏览器、无 Playwright、无 Camoufox。</p>

> [!WARNING]
> 免责声明：本项目仅供个人学习、技术研究与非商业性技术交流使用。严禁用于任何商业用途、批量滥用、规模化调用，或违反 OpenAI 服务条款及当地法律法规的行为。使用者自行承担全部风险，包括账号被限制、封禁及相应法律责任。请勿使用自己的重要账号或高价值账号进行测试。

技术交流群：259844673

<img width="280" height="265" alt="image" src="https://github.com/user-attachments/assets/a24f9f9b-0dfa-440e-8ee8-6e461d03eeea" />

## 核心特性

| 能力 | 说明 |
|---|---|
| **多邮箱来源** | Remail 商业邮箱 API / Outlook 接码池 / CF Worker 自建 catch-all / iCloud 中继，WebUI 下拉切换，加新邮箱前端零改动 |
| **Remail 订单管理** | 下单、取件、禁用、删除、钱包余额、订单复用，长效订单可重复使用（上限 10 次），短效订单用尽即失效 |
| **代理池** | 存后端 SQLite，支持静态/动态类型区分、单代理注册配额上限（默认 50）、CF 拦截自动停用、IPv6 出口检测、失败归还预占配额 |
| **全自动循环** | 多 worker 并发（1-20），每 worker 独立代理，失败自动换号/换代理，网络错误熔断 |
| **错误分类** | `network` / `cf_blocked` / `mail_limited` 等分类，区分「换代理」与「停用代理」「换邮箱」的处置策略 |
| **凭证完整落库** | 保存 `access_token` / `refresh_token` / **原始 session JSON**，多账号批量导出 |
| **配置全部后端化** | 表单偏好、主题、代理池、Remail 订单全在 SQLite，换机器、清缓存后依然保留 |
| **Docker 部署** | 多阶段构建，最终镜像约 330MB，数据目录外置，容器重建不丢数据 |

## 快速开始

### Docker 运行（推荐）

```bash
git clone https://github.com/Regert888/gpt-outlook-register.git
cd gpt-outlook-register
docker compose up -d --build
```

- Web 面板：`http://服务器IP:8765`
- 数据目录：`./data`（挂载到容器 `/app/data`），内含 `webui.db` 与 `logs/`

> ⚠️ **容器内代理地址**：容器里的 `127.0.0.1` 指的是容器自己，不是宿主机。
> 要用宿主机上的代理，填 `host.docker.internal:<端口>`；compose 已配置 `host-gateway`，
> Linux 下也能解析。宿主机代理软件若只监听 `127.0.0.1`，容器连不上——需改成监听 `0.0.0.0`。

更新到新版本：

```bash
cd gpt-outlook-register
git pull
docker compose up -d --build
```

`./data` 在宿主机上，`git pull` 与容器重建都不会丢数据。

### 本地运行

```bash
git clone https://github.com/Regert888/gpt-outlook-register.git
cd gpt-outlook-register
pip install -r requirements.txt
python start_webui.py
# 浏览器自动打开 http://127.0.0.1:8765/
```

公网启动：

```bash
python start_webui.py --host 0.0.0.0 --port 8765
```

**Linux 服务器后台运行**（推荐 `screen`，关 SSH 不杀进程）：

```bash
apt install -y screen        # CentOS/RHEL: yum install -y screen
screen -S webui
python start_webui.py --host 0.0.0.0 --port 8765
# 按 Ctrl+A 再按 D 离开，进程继续运行
# 重新进入：screen -r webui
```

> ⚠️ **必须装 Node.js（≥18）**。纯 Python 路径只能过 Sentinel `/req` 表层校验，
> OpenAI 服务端跑真 `sdk.js` 深层校验时会判定为非浏览器 → **OTP 邮件 silent-drop**
> （HTTP 200 但不下发验证码）。要稳定收到验证码，**强烈建议装 node**。

```bash
# Ubuntu/Debian
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs
node --version  # 需要 >= 18
```

### 已有项目安全升级

```bash
cd gpt-outlook-register

# 1. 🔴 备份数据库（重要！）
cp data/webui.db data/webui.db.backup

# 2. 拉取最新代码
git pull

# 3. 重启
python start_webui.py
```

数据库在 `data/` 下（Docker 部署）或 `webui/webui.db`（源码运行），已在 `.gitignore` 中，
`git pull` 不会覆盖或删除。新版本会自动执行 migration，**不会删除**已有数据。

## WebUI 菜单总览

| 分组 | 菜单 | 路径 | 说明 |
|---|---|---|---|
| 概览 | 仪表盘 | `/` | 号池统计与快捷入口 |
| 注册 | 导入邮箱 | `/import` | 批量粘贴邮箱 4 段格式入库 |
| 注册 | 单次注册 | `/register` | 可视化触发单次注册，SSE 实时日志 |
| 注册 | 全自动批量 | `/auto` | 多 worker 并发循环 |
| 注册 | **代理池** | `/proxy` | 代理管理、类型标记、配额、IPv6 检测 |
| 数据 | 邮箱列表 | `/pool` | 号池状态管理 |
| 数据 | **Remail 服务** | `/mails` | Remail 订单管理 |
| 数据 | 注册结果 | `/registered` | 凭证复制与批量导出 |
| 数据 | 运行记录 | `/runs` | 历史运行日志 |
| 配置 | 邮箱配置 | `/settings/mail` | 邮箱来源切换与 provider 配置 |
| 配置 | 接码配置 | `/settings/sms` | SMS 接码平台配置 |
| 配置 | 自动导出 | `/settings/export` | 导出目标与格式配置 |

> ⚠️ 所有界面数据都存后端 SQLite（`settings.prefs` / `proxy_pool` / `remail_orders`），
> 不再依赖浏览器 localStorage —— 换机器、清缓存后依然保留。

## 邮箱来源

WebUI「⚙️ 设置 → 邮箱配置」里下拉切换，后端 provider 声明配置项，前端动态渲染。

| 来源 | 说明 | 适用场景 |
|---|---|---|
| **Remail** | 商业邮箱 API，按项目下单取件 | 有 API Key 与项目 ID，最省事 |
| **Outlook 接码池** | IMAP XOAUTH2 取 OTP（4 段格式导入） | 手上已有微软邮箱接码号 |
| **CF 自建邮箱** | `dreamhunter2333/cloudflare_temp_email` 兼容 | 自建 catch-all，不用买号 |
| **iCloud 中继** | 隐藏邮箱中继 | iCloud 隐藏邮件地址 |

### Remail 邮箱服务

「Remail 服务」菜单（`/mails`）提供完整的订单管理界面。

**配置项**（邮箱配置页填写）：

| 配置项 | 说明 |
|---|---|
| `remail_api_key` | API Key（oauth2 key） |
| `remail_project_id` | 项目 ID |
| `remail_email_suffix` | 邮箱后缀，自由输入（如 `outlook`） |
| `remail_service_mode` | 服务模式：`purchase`（长效）/ `code`（短效） |
| `remail_supply_policy` | 库存策略：`private_first`（优先自有库存）/ `public_only`（仅公共库存） |

**长效 vs 短效的关键差异**：

| | 长效（purchase） | 短效（code） |
|---|---|---|
| 收件次数 | 可重复收（实测 2 封） | **只投递 1 封** |
| 注册流程 | 正常设密码 + 收码 | **跳过设密码**，用服务端首次 OTP |
| 订单状态 | 保持 `active` | 用尽后置为 `consumed` |
| 复用 | 失败可复用，上限 10 次 | 不复用（`mail_limited` 分类） |

> ⚠️ 短效邮箱**不能设密码**：设密码会让服务端切到 `email_otp_send` 页，
> 提交邮箱时自动发的那封 OTP 立即失效，而短效邮箱只保留 1 封邮件，补发收不到 → 死锁。
> 因此短效模式全程只收 1 封验证码，账号没有密码。

**订单复用条件**（长效订单，未关联账号时）：`purchase` 模式 + 无 token 关联 + 有 serviceToken + 状态有效 + 后缀/项目匹配 + 尝试次数 < 10。

## 代理池

「代理池」菜单（`/proxy`）。

| 能力 | 说明 |
|---|---|
| 类型区分 | `static`（固定出口 IP）/ `dynamic`（每次连接换 IP） |
| 配额上限 | 单代理成功注册数上限，默认 50，`0` = 不限 |
| CF 拦截处理 | **静态**代理连续被 403 两次 → 自动停用；**动态**代理只计数不停用（换 IP 可能就通过） |
| IPv6 检测 | 访问 `cloudflare.com/cdn-cgi/trace` 判断出口是否为 IPv6 |
| 并发安全 | 选中即预占配额，失败归还，避免多 worker 抢同一代理 |
| 异常释放 | 账号删除/标注异常时自动释放其占用的配额 |

> ⚠️ **动态代理的注意事项**：注册链路跨 `chatgpt.com` 与 `auth.openai.com` 两个域名，
> 是两条独立隧道，纯随机轮换的动态代理会让「种 cookie 的 IP」与「提交注册的 IP」不一致，
> 极易触发 409 `invalid_state` 与风控。**建议使用粘性会话（sticky session）代理**——
> 让一次注册全程固定同一个出口 IP。

## 导出格式

「导出配置」菜单可选择格式，批量导出多账号。

| 格式 | 文件名 | 说明 |
|---|---|---|
| `access_token` | `AT` | 仅 access_token |
| 邮箱----密码 | `账号密码.txt` | 基础账号信息 |
| 邮箱----密码----2FA | `账号密码2FA.txt` | 含 2FA secret（**仅下发一次，取不回**） |
| 邮箱----密码----2FA----取件url | `账号密码2FA取件url.txt` | 含取件链接（等同收件权限） |
| **access_token----代理** | `AT_代理.txt` | **直供 chatgpt2api 导入**，让注册与使用同一个 IP |
| **原始 session JSON** | `session_json.txt` | `/api/auth/session` 完整响应，一行一个，含 token 与 expires |

「已注册账号」页每个账号都有独立的**复制原始 session_json** 按钮。
> ⚠️ 存量账号没有 `session_json`（该功能后加），按钮会禁用。

## 配套：chatgpt2api

把 `access_token----代理` 导出结果直接粘进 [chatgpt2api](https://github.com/qll-yin/chatgpt2api)
的「号池管理 → 导入账号 → Access Token」，即可让每个账号在注册和使用阶段走同一 IP。

## 完整协议链路

```
register_outlook.py
    └── AuthFlow(cfg).run_register(mail_provider)
            ├── [1/10] GET  chatgpt.com/api/auth/csrf                       → csrf_token
            ├── [2/10] POST chatgpt.com/api/auth/signin/openai              → auth_url (含 client_id)
            ├── [3/10] GET  auth.openai.com/authorize?...                   → device_id (oai-did cookie)
            ├── [4/10] POST sentinel.openai.com/backend-api/sentinel/req    → sentinel_token (PoW)
            ├── [5/10] POST auth.openai.com/api/accounts/authorize/continue → 判定 signup vs 已有账号
            │           (screen_hint=signup, openai-sentinel-token=...)
            ├── [5.5]  POST auth.openai.com/api/accounts/user/register      → 注册密码（短效邮箱跳过）
            ├── [6/10] GET  auth.openai.com/api/accounts/email-otp/send     → 触发 OTP 邮件
            │           或 POST .../passwordless/send-otp
            │           或 POST .../email-otp/resend
            ├── mail_provider.wait_for_otp() ←──── 拉取 OTP
            ├── [7/10] POST auth.openai.com/api/accounts/email-otp/validate → 验证 OTP
            ├── [8/10] POST auth.openai.com/api/accounts/create_account     → 填 name/birthdate
            ├── [9/10] follow_redirect_chain()                              → 抓 callback URL (含 code)
            ├── [10/10] GET chatgpt.com/api/auth/session                    → access_token + 完整 session JSON
            └── [可选] POST auth.openai.com/oauth/token (Codex PKCE)        → refresh_token
```

**409 `invalid_state` 自动重试**：最多 3 次尝试，命中 409 时重建 session、换出口 IP 并**复用原邮箱**（邮箱已付费，不额外花钱）。被 CF 403 拦截归类为 `cf_blocked`，只换代理，不触发整批熔断。

## 命令行模式（单号注册）

```bash
python register_outlook.py 'email----password----client_id----refresh_token'
```

Outlook 4 段接码格式：

```
email----password----client_id----microsoft_refresh_token
```

例：

```
charles@outlook.jp----<pwd>----9e5f94bc-e8a4-...----M.C538_BAY.0.U.-Cs...
```

> `client_id` 必须具备 v2 endpoint 的 `https://outlook.office.com/IMAP.AccessAsUser.All offline_access` scope。
> 实测供应商卖的接码号若用 Thunderbird client_id 通过 Device Code Flow 重拿的 refresh_token 才有 IMAP scope。

成功输出 `account_<email>.json`：

```json
{
  "email": "...",
  "password": "...",
  "session_token": "...",   // __Secure-next-auth.session-token cookie
  "access_token": "...",    // /api/auth/session 返回
  "device_id": "...",       // oai-did cookie
  "csrf_token": "...",
  "id_token": "...",
  "refresh_token": "...",   // Codex OAuth 拿的 RT
  "cookie_header": "..."    // chatgpt.com 全 cookie 拼接
}
```

### 单独测试取码

```bash
python mail_outlook.py 'email----password----client_id----refresh_token'
# → OTP: 123456  （需要 OpenAI 已发过码进该邮箱）
```

## SMS 接码集成

OpenAI 注册有概率被风控跳到 `add-phone` 步骤，**不绑手机号就拿不到 refresh_token**。
WebUI「📱 接码配置」Tab 启用后，命中 add-phone 会自动租号 + 等 SMS + 自动验证。

| 平台 | 网址 | 特色 |
|---|---|---|
| **SmsBower** | smsbower.page | V2 API + auto-resend |
| **HeroSMS** | hero-sms.com | 180+ 国家，SMS-Activate 继任平台 |

配置步骤：注册平台账号 → 充值 → 拿 API Key → WebUI「📱 接码配置」填 API Key + 选国家 → 保存 → 点「测试余额」确认连通。

| 配置项 | 说明 |
|---|---|
| `sms_enabled` | 总开关；关闭时回退到 `OPENAI_PHONE_NUMBER` 环境变量路径 |
| `sms_provider` | herosms / smsbower / sms_activate |
| `sms_country` | 国家代码或 ID，默认 `52`（Thailand） |
| `sms_service` | OpenAI = `dr` |
| `sms_max_price` | 单号最高价格 |
| `sms_reuse_phone` | 同号复用 |
| `sms_phone_success_max` | 同号最多复用 N 次 |
| `sms_auto_country` | 自动选最优国家（限白名单） |

> ⚠️ **WhatsApp vs SMS**：OpenAI 自 2025 年起对大部分国家改用 WhatsApp 验证。
> 纯 SMS 路径目前**只有泰国（country_id=52）确认可用**，其它国家可能抽到 WhatsApp 号导致收不到 SMS → 超时 → 退款。
> 自动选国家时会优先挑 OpenAI-SMS 白名单内的国家，非白名单会给出警告（不阻止）。

> ⚠️ **refresh_token 开关与接码联动**：WebUI 的「获取 refresh_token」开关默认**关闭**。
> 关闭时只打印日志不发起 Codex OAuth。短效邮箱只保留 1 封邮件，
> 开启 Codex 链会触发**第二次收件**并偷走唯一的收件机会——已加行为层拦截兜底。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY` | - | 出口代理 URL，例 `socks5://user:pass@host:port`（curl_cffi 自动 socks5→socks5h） |
| `OTP_TIMEOUT` | `60` | OTP 等待秒数（实际下限 30） |
| `WEBUI_ALLOW_LOGIN` | `0` | `1`=邮箱被识为已注册时走 OTP login；`0`=fast-fail 抛错 |
| `SKIP_OAUTH_TOKEN_EXCHANGE` | `0` | `1`=跳过 OAuth refresh_token 交换 |
| `OAUTH_CODEX_RT_EXCHANGE` | `1` | `1`=尝试 Codex OAuth 拿 refresh_token |
| `OAUTH_REFRESH_ONLY` | `0` | `1`=只要 refresh_token，跳过 session 步骤 |
| `OPENAI_SENTINEL_NODE_PATH` | `node` | node 二进制路径 |
| `LOGIN_PASSWORD` | - | 已有账号 login_password 分支用的密码（不设则取 email 去 @） |
| `AUTH_HTTP_TRACE` | `0` | `1`=打印每次 HTTP 请求 method/url/status/cookies（调试用） |
| `AUTH_TRACE_DUMP` | `0` | `1`=写完整 HTTP request/response 到 `outputs/auth_trace_*.jsonl` |
| `OPENAI_PHONE_NUMBER` | - | 手动 add-phone 时的号码（用 `,` 分隔多个候选）。**未启用 SMS 接码时用** |
| `OPENAI_PHONE_OTP` | - | 静态 OTP 验证码（调试用，6 位数字） |
| `OPENAI_PHONE_OTP_CMD` | - | 动态获取手机 OTP 的 shell 命令（stdout 含 6 位数字即命中） |
| `OPENAI_PHONE_OTP_TIMEOUT` | `180` | add-phone 等 SMS 超时（秒） |
| `WEBUI_DB_PATH` | `data/webui.db` | SQLite 路径（Docker 里为 `/app/data/webui.db`） |
| `WEBUI_LOG_DIR` | `data/logs` | 运行日志目录（Docker 里为 `/app/data/logs`） |

## 文件清单

### 核心库（命令行 + WebUI 共用）

| 文件 | 作用 |
|---|---|
| `register_outlook.py` | **命令行入口**：4 段格式 → `AuthFlow.run_register` → 写出账号 JSON |
| `auth_flow.py` | **纯协议核心**：csrf → authorize → sentinel → signup → otp → create_account → callback → token exchange |
| `http_client.py` | `curl_cffi` Chrome136 TLS 指纹包装（失败自动降级 chrome124 / chrome120） |
| `config.py` | 极简 Config（只留 `proxy` 字段） |
| `sentinel.py` | OpenAI Sentinel Token 纯 Python PoW（FNV-1a 32-bit） |
| `sentinel_quickjs.py` | Sentinel Token QuickJS 路径（跑 OpenAI sdk.js，需要 node） |
| `openai_sentinel_quickjs.js` | QuickJS 路径用的 sdk.js wrapper |
| `sms_provider.py` | **SMS 接码 provider**：BaseSmsProvider + SmsActivate / HeroSMS / SmsBower + PhoneCallbackController |

### 邮箱 Provider（`mail_providers/`）

| 文件 | 作用 |
|---|---|
| `base.py` | Provider 基类：配置声明、错误分类、`wait_for_otp(earliest=...)` 统一取件接口 |
| `remail.py` | **Remail 商业邮箱 API**：下单 / 取件 / 自检 / 长效订单复用 / 短效单次取码 |
| `outlook.py` | Outlook IMAP XOAUTH2 取 OTP（refresh_token 续期 + 多 folder + tm1 影子过滤） |
| `cf_temp.py` | CF Worker 自建邮箱（`cloudflare_temp_email` 兼容；OTP 抽取防误判） |
| `icloud_relay.py` | iCloud 隐藏邮箱中继 |

> 根目录的 `mail_outlook.py` / `mail_cf.py` 是**兼容转发壳**（实现已迁到 `mail_providers/`），
> 仅保留旧 import 路径可用，新代码请用 `create_mail_provider(kind, ...)`。

### WebUI

| 文件 | 作用 |
|---|---|
| `start_webui.py` | 一键启动脚本（自动装依赖 + 打开浏览器） |
| `webui/app.py` | FastAPI 主程序（路由 + SSE 流式日志） |
| `webui/db.py` | SQLite：号池 / 注册结果 / **代理池** / **Remail 订单** / 偏好设置 |
| `webui/registrar.py` | 注册任务 worker（独立线程 + 日志回调 + 错误分类 + 订单关联） |
| `webui/auto_loop.py` | 全自动循环（多 worker、动态挑代理、CF 分类、熔断） |
| `webui/export_formats.py` | 多格式批量导出定义 |
| `webui/frontend/` | Vue 3 + Element Plus 前端源码 |

## 技术细节

### Sentinel Token 深层校验

- **纯 Python sentinel**：`/sentinel/req` 返 200 OK，`/authorize/continue` 也返 200 OK，
  但 OpenAI 服务端做 OTP 派发前会跑**真 sdk.js** 校验 token，纯 Python PoW 会被判为非浏览器 → 邮件 silent-drop。
- **QuickJS sentinel（默认启用）**：跑 OpenAI 真 `sdk.js`（自动下载缓存到 `/tmp/openai-sentinel-demo/<ver>/sdk.js`），返回真实浏览器同款 token，OTP 派发正常。
- 没装 node 也能跑，但只能用于纯协议探测，**不能真发码**。

### Outlook 收件细节

- **Junk 邮件**：首次发码给陌生收件人常被 Outlook 反垃圾分到 Junk，脚本扫多 folder（INBOX / Junk / Junk Email / Spam）。
- **tm1.openai.com 影子发码**：当前坏掉的发码域，所有账号都返固定 OTP=493682 → verify 401。已硬过滤。
- **refresh_token 自动滚动续期**：Microsoft access_token 1 天有效，模块每 3000s 自动续期。

### OpenAI 反欺诈

- **已有账号分支**：邮箱被识为已注册时，默认 fast-fail 抛错（防 honeypot）。
  设 `WEBUI_ALLOW_LOGIN=1` 改走 OTP login 拿凭证。
- **`authorize/continue` 已 trigger 发码**：passwordless 模式下 OpenAI 已经发了码，
  此时**必须只 resend**，不能再调 `email-otp/send`（会新建 challenge → 旧 OTP 失效 → `wrong_email_otp_code`）。
  代码里的 `kickoff_otp_delivery` 已根据 `_is_existing_account` 自动切分支。
- **批量注册次日存活率约 2%**（原项目反欺诈研究数据），单号 / 偶发使用没问题。
- **409 `invalid_state`**：同一条链路重复触发的信号，会重建 session、换出口 IP 并复用原邮箱重试（最多 3 次）。

### 网络层

- **curl_cffi SOCKS5**：默认把 `socks5://` 规范化成 `socks5h://`（DNS 走代理解析），减少 TLS 握手异常。
- **TLS 指纹**：默认 `chrome136`；握手失败自动尝试 `chrome124` / `chrome120` 兜底。
- **Cloudflare 403**：`get_csrf_token` 内置 3 次重试 + 指数 backoff。

### SMS 接码退款

HeroSMS 购买后 2 分钟内不可取消，取消后 20 分钟自动退款到余额；SmsBower 可立即取消退款。号码不可用时立刻换号，HeroSMS 的废号等 20 分钟后自动退回。

OTP validate 失败（phone-otp/validate 401）时自动 resend 一次再等新码。

## 测试

```bash
python test_integration.py
python test_remail_provider.py
python test_ui_fixes.py
```

测试使用独立的临时数据库，**不会污染生产库**。

## License

本项目采用 **AGPL-3.0** 许可证，完整条款见 [LICENSE](LICENSE)。

- **个人学习和研究**：可自由使用。
- **二次开发**：衍生作品须以相同许可证**开源**，并保留原作者署名和本项目地址 <https://github.com/Regert888/gpt-outlook-register>。
- **AGPL 特有条款**：通过网络提供服务（例如把本项目 WebUI 架在服务器上给别人用）同样视为分发，**必须向使用者提供对应的完整源码**。

二次开发欢迎，但请带上出处，谢谢。
