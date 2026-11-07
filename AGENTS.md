# AGENTS.md

## 项目定位

Regix 是纯协议注册平台，没有浏览器自动化。WebUI、CLI 和自动跑号必须复用同一套业务层：`regix/services/*` 负责流程编排，`regix/openai/*` 负责协议实现，`regix/storage/facade.py` 是唯一推荐的持久化入口。不要在路由或 CLI 里复制注册、TOTP、代理配额或邮箱号池状态机。

## 开发规则

- 新能力先落在 service/provider 层，再让 FastAPI router 和 CLI 做薄封装。router 只做请求校验和状态码转换，CLI 只做参数解析、输出格式和调用 service。
- 邮箱 provider 通过 `regix/providers/mail/__init__.py` 的注册机制扩展。加 provider 时保持 `pooled`、导入格式、配置字段和失败语义显式声明。
- SQLite 访问必须经过 `regix.storage.facade`。涉及 `outlook_accounts`、`registered`、`runs`、`proxy_pool` 的行为改动要考虑并发 claim、失败释放、成功确认和幂等性。
- TOTP secret、密码、token、邮箱 refresh token、代理凭证和 SMS API key 默认不能写入日志、测试输出或终端响应。API/CLI 返回先经过掩码，只有用户显式要求展示时才输出。
- TOTP 绑定必须继续使用 `regix/services/two_factor.py`。secret 只下发一次，拿到后必须尽早调用 `save_totp_early`，不得等待后续手机验证或导出完成。
- 代理池选中即预占配额。失败要释放预占；成功由现有结算逻辑确认。不得因为测试绕过代理池配额。
- Sentinel 真实 SDK 路径在 `regix/openai/quickjs.py` 和 `openai_sentinel_quickjs.js`。不要降级到纯 Python synthetic token；不要删除 `solve` 中刻意保留的行为采集时间。

## CLI 约定

`regix register` 是 WebUI 单次注册的等价入口，默认读取 WebUI 偏好；`--totp/--no-totp` 可以覆盖。旧的四段 Outlook 直跑入口保留为 `regix legacy` / `regix-legacy`。需要使用未单独封装的 WebUI 功能时，用 `regix api METHOD /api/...` 透传，而不是复制 router 逻辑。

## 验证

常规改动至少运行：

```bash
.venv/bin/python -m compileall -q regix
.venv/bin/python -m pytest
```

涉及前端时另跑 `npm --prefix webui/frontend run build`，并确认构建产物已同步到 `webui/static`。涉及 Sentinel 时运行 `node --check regix/openai/openai_sentinel_quickjs.js`，并用本地 SDK cache 做 requirements 基准；输出后 Node 必须立刻退出。涉及真实注册时先获得明确授权，并要求开启 TOTP；测试后只报告 run 状态和字段长度，不报告 secret。

涉及注册耗时、轮询间隔、重试间隔或 Sentinel 行为节奏时，先阅读
`docs/PERFORMANCE.md`。至少收集 5 个真实成功样本再修改默认节奏；本地性能测试
不得把 `OPENAI_SENTINEL_BEHAVIOR_MS=0` 用到生产注册。

## 工作区注意

本仓库经常有用户未提交改动。修改前先看 `git status --short`，只触碰当前任务需要的文件；不要回退、格式化或覆盖无关改动。新增测试优先放在 `tests/`，复用 `tests/conftest.py` 的临时数据库隔离。
