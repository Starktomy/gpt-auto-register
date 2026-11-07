# Regix project structure refactor plan

## Status

The refactor has been implemented without compatibility shims. `regix/` now owns
the HTTP/fingerprint layer, mail and SMS providers, SQLite repositories,
registration services, split API routers, CLI entry points, and OpenAI protocol
mixins. The repository exposes only `regix` and `regix-webui` entry points;
container startup uses `regix.api.app:app` directly.

## 结论

当前项目可以继续运行，但已经到了“单文件承载过多业务”的阶段。建议分 6 个阶段迁移到 `regix/` 包结构，先做无行为变化的目录拆分，再逐步拆注册协议、WebUI 和数据库层。不要一次性移动全部文件，否则当前 409/invalid_state、短效邮箱、代理池、2FA 这些刚修过的链路很难回归。

## 现状问题

| 模块 | 行数 | 问题 |
| --- | ---: | --- |
| `auth_flow.py` | 3,983 | 同时包含 OAuth、注册、登录、OTP、手机验证、2FA、Session/Token、HTTP headers、重试和最终编排。`AuthFlow` 一个类约有 90 个方法。 |
| `webui/app.py` | 1,549 | 约 45 个 API route 集中在一个 FastAPI app 里，Basic Auth、请求模型、业务调用、导出、检测逻辑混在一起。 |
| `webui/db.py` | 2,159 | 账号池、registered、代理、Remail 订单、runs、设置、导出配置都通过同一个全局 DB facade 操作。 |
| `sms_provider.py` | 1,069 | SMS 平台协议、国家映射、工厂、两阶段回调控制器混在一个文件。 |
| `webui/registrar.py` | 824 | 注册 worker 的日志、错误分类、线程管理、状态落库和外部导出耦合。 |

其他结构性问题：

1. 根目录同时有正式模块、兼容 wrapper、测试、账号 JSON 和静态资源。
2. `mail_providers/remail.py` 反向依赖 `webui.db`，让底层邮箱 provider 依赖了 WebUI 持久层。
3. `webui/app.py`、`webui/registrar.py`、`webui/two_factor.py` 多处手工 `sys.path.insert()`。
4. Dockerfile 硬编码逐个 COPY 根目录 `.py`，每次加模块都容易漏。
5. 注册账号 JSON 默认落在仓库根目录，运行产物和源码没有隔离。
6. 部分测试靠正则检查源码文本，重构移动代码时即使行为正确也可能误报。
7. `test_integration.py` 依赖外部 `/root/chatgpt2api`，当前环境会失败；这类外部依赖应显式 skip 或 mock。

## 目标结构

推荐先使用仓库内包结构，等 import 稳定后再考虑 `src/` layout：

```text
.
├── regix/
│   ├── __init__.py
│   ├── config.py
│   ├── http/
│   │   ├── client.py
│   │   └── errors.py
│   ├── fingerprint/
│   │   └── generator.py
│   ├── sentinel/
│   │   ├── quickjs.py
│   │   └── openai_sentinel_quickjs.js
│   ├── providers/
│   │   ├── mail/
│   │   │   ├── base.py
│   │   │   ├── registry.py
│   │   │   ├── cf_temp.py
│   │   │   ├── icloud_relay.py
│   │   │   ├── outlook.py
│   │   │   └── remail.py
│   │   └── sms/
│   │       ├── base.py
│   │       ├── errors.py
│   │       ├── smsbower.py
│   │       ├── countries.py
│   │       ├── factory.py
│   │       └── callback.py
│   ├── openai/
│   │   ├── models.py
│   │   ├── client.py
│   │   ├── oauth.py
│   │   ├── signup.py
│   │   ├── login.py
│   │   ├── challenges.py
│   │   ├── session.py
│   │   └── registration.py
│   ├── storage/
│   │   ├── database.py
│   │   ├── migrations.py
│   │   ├── accounts.py
│   │   ├── registered.py
│   │   ├── proxies.py
│   │   ├── mail_orders.py
│   │   ├── runs.py
│   │   └── settings.py
│   ├── services/
│   │   ├── registration.py
│   │   ├── auto_loop.py
│   │   ├── export.py
│   │   └── two_factor.py
│   ├── api/
│   │   ├── app.py
│   │   ├── security.py
│   │   ├── accounts.py
│   │   ├── proxies.py
│   │   ├── registration.py
│   │   ├── runs.py
│   │   ├── mail.py
│   │   ├── sms.py
│   │   ├── exports.py
│   │   └── static.py
│   └── cli/
│       └── register_outlook.py
├── web/
│   └── frontend/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── conftest.py
├── data/
├── docs/
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

包名用 `regix`，不要用过于泛化的 `core`、`utils`、`common` 作为顶层包名。

## 阶段计划

### Phase 0：冻结行为和建立回归基线

1. 创建重构分支，例如 `refactor/project-structure`。
2. 先提交或暂存当前 409/WebUI/passwordless 修复，不要把结构重构和业务修复混在同一个提交里。
3. 运行现有测试：
   ```bash
   .venv/bin/python test_register_409_fixes.py
   .venv/bin/python test_remail_provider.py
   .venv/bin/python test_ui_fixes.py
   .venv/bin/python test_integration.py
   ```
4. 记录当前已知结果：
   - `test_register_409_fixes.py`：通过。
   - `test_remail_provider.py`：通过。
   - `test_ui_fixes.py`：1 个旧断言仍把 `invalid_state` 期望为 `account`，和当前“网络/会话错误可重试”的修复方向冲突，需要更新测试期望。
   - `test_integration.py`：`/root/chatgpt2api` 不存在导致 1 项失败，应改为条件 skip。
5. 用一个临时数据库做 WebUI smoke test，不要触碰 `webui/webui.db`。

### Phase 1：建立包骨架和兼容层

只移动稳定、低耦合模块，不动 `auth_flow.py` 的大类：

```text
regix/
├── config.py
├── http/client.py
├── fingerprint/generator.py
└── sentinel/
```

保留兼容入口：

```python
# http_client.py
from regix.http.client import *  # noqa: F401,F403
from regix.http.client import create_http_session, USER_AGENT
```

```python
# fingerprint.py
from regix.fingerprint.generator import *  # noqa: F401,F403
```

这一阶段要满足：

- `python start_webui.py` 不变。
- `python register_outlook.py` 不变。
- Docker 不需要理解新结构也能工作。
- 所有业务行为无变化。
- 静态测试中的 `fingerprint.py`、`auth_flow.py` 文本断言暂时仍然有效。

### Phase 2：拆 SQLite 持久层

把 `webui/db.py` 拆成 storage repositories，并暂时保留 `webui/db.py` 作为 facade。

建议拆分：

| 新文件 | 职责 |
| --- | --- |
| `storage/database.py` | 连接、事务、SQLite PRAGMA、路径解析 |
| `storage/migrations.py` | `init_db()` 和 schema migration |
| `storage/accounts.py` | 号池导入、领取、释放、重置、删除 |
| `storage/registered.py` | registered 结果、密码/TOTP/session、异常标记 |
| `storage/proxies.py` | 代理池、占用、成功失败计数、CF 拦截 |
| `storage/mail_orders.py` | Remail 订单、复用、禁用、删除 |
| `storage/runs.py` | runs 记录 |
| `storage/settings.py` | 站点偏好、mail/sms/export 配置、secret settings |

优先做两件事：

1. `webui.db` 的 `_conn()` 从“全局隐式连接”改为 `Database` 对象。
2. 移除 `mail_providers/remail.py -> webui.db` 的反向依赖。Remail provider 应接收 repository 或持久化回调，不能直接 import WebUI 模块。

迁移期兼容：

```python
# webui/db.py，临时 facade
from regix.storage.database import default_database
from regix.storage.accounts import AccountsRepository
from regix.storage.registered import RegisteredRepository
```

API 层可以暂时继续调用 `webui.db`，等 repositories 稳定后切换。

### Phase 3：拆 provider 层

把邮箱和 SMS provider 归入统一 provider 包：

```text
regix/providers/mail/
regix/providers/sms/
```

保留：

```python
mail_providers/__init__.py -> from regix.providers.mail import *
mail_cf.py -> 兼容 wrapper
mail_outlook.py -> 兼容 wrapper
sms_provider.py -> 兼容 wrapper
```

拆 `sms_provider.py`：

| 新文件 | 内容 |
| --- | --- |
| `providers/sms/base.py` | `BaseSmsProvider`、通用异常 |
| `providers/sms/smsbower.py` | SmsBower HTTP 协议 |
| `providers/sms/countries.py` | 国家列表、中文映射 |
| `providers/sms/factory.py` | `create_sms_provider()` |
| `providers/sms/callback.py` | `PhoneCallbackController` |

邮箱 registry 也应该收敛到一个明确的 `registry.py`，避免 `mail_providers/__init__.py` 同时负责文档、注册表和 import 副作用。

### Phase 4：拆 WebUI 后端

`webui/app.py` 改成 app factory + APIRouter：

```python
# regix/api/app.py
def create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(accounts_router)
    app.include_router(proxies_router)
    app.include_router(registration_router)
    app.include_router(mail_router)
    app.include_router(sms_router)
    app.include_router(exports_router)
    app.include_router(auto_loop_router)
    app.include_router(static_router)
    return app
```

route 拆分：

| router | 覆盖现有 API |
| --- | --- |
| `accounts.py` | import、list、claim/reset/delete、stats、preferences |
| `proxies.py` | proxy test、pool CRUD、IPv6、CF blocked |
| `registration.py` | 单次注册、run stream、registered 管理 |
| `mail.py` | providers、mail config、Remail orders |
| `sms.py` | SMS config、test、countries |
| `exports.py` | export config、格式、导出、panel |
| `auto_loop.py` | start/pause/resume/stop/status/stream |
| `static.py` | `/` 和静态文件 |

同时拆服务层：

| 服务 | 来源 |
| --- | --- |
| `services/registration.py` | `webui/registrar.py` 的 worker、日志、状态推送 |
| `services/error_taxonomy.py` | `classify_error()` |
| `services/auto_loop.py` | `webui/auto_loop.py` |
| `services/export.py` | `webui/exporter.py` |
| `services/two_factor.py` | `webui/two_factor.py` |

规则：

- route 只做参数校验、调用 service、返回响应。
- service 不 import FastAPI。
- provider 不 import WebUI API 或 DB facade。
- Basic Auth 只放在 `api/security.py`，通过 dependency 注入。

### Phase 5：拆 `auth_flow.py`

这是最大风险点，必须最后做。`AuthFlow` 对外 API 暂时保持不变，内部用 mixin 或协作对象拆分。

建议按业务域拆：

| 新模块 | 负责方法域 |
| --- | --- |
| `openai/models.py` | `AuthResult`、错误类型、注册结果 |
| `openai/client.py` | HTTP session、headers、cookie、trace、TLS 错误识别 |
| `openai/oauth.py` | PKCE、authorize URL、callback、token exchange |
| `openai/signup.py` | signup、register password、OTP、create account、workspace |
| `openai/login.py` | password verify、protocol login、existing credentials |
| `openai/challenges.py` | TOTP/MFA、add phone、SMS loop |
| `openai/session.py` | auth session、callback session、refresh |
| `openai/registration.py` | `run_register()` 总编排 |

推荐实现方式：

1. 第一阶段用 `AuthFlowClientMixin`、`AuthFlowOAuthMixin`、`AuthFlowSignupMixin` 等组合成 `AuthFlow`。
2. 对外继续导出 `AuthFlow` 和 `run_register()`，`auth_flow.py` 变成兼容 facade。
3. mixin 稳定后再改成显式协作对象，例如 `OpenAIClient`、`OAuthClient`、`ChallengeHandler`。
4. `run_register()` 里的长流程拆成 step 函数，但不要先改状态机和重试语义。
5. `invalid_state` 当前按网络/会话可重试处理，迁移时必须保留这个行为。

### Phase 6：清理兼容层和运行产物

1. 根目录 wrapper 只保留仍被外部脚本依赖的入口。
2. 账号 JSON 移到 `data/output/` 或环境变量指定目录，并加入 `.gitignore`。
3. 删除已经无人使用的 `mail_cf.py`、`mail_outlook.py` wrapper。
4. Dockerfile 改成：
   ```dockerfile
   COPY regix/ ./regix/
   COPY web/frontend/ ./web/frontend/
   ```
5. `pyproject.toml` 增加 build-system 和 console scripts：
   ```toml
   [project.scripts]
   regix = "regix.cli.register_outlook:main"
   gpt-webui = "regix.api.cli:main"
   ```
6. 前端源码最终从 `webui/frontend` 迁到 `web/frontend`，构建产物放 `regix/api/static` 或继续使用独立 `web/static`。
7. 最后移除旧的 `auth_flow.py`、`webui/db.py`、`webui/app.py` 兼容 facade。

## 每阶段验收

每个 phase 完成后至少检查：

```bash
.venv/bin/python -m compileall -q regix webui
.venv/bin/python test_register_409_fixes.py
.venv/bin/python test_remail_provider.py
.venv/bin/python test_ui_fixes.py
.venv/bin/python test_integration.py
```

再手工验证：

1. WebUI 首页加载，Basic Auth 正常。
2. 号池导入、registered 列表、代理池列表可用。
3. Remail 自检通过。
4. SMS 国家列表不触发网络请求时能正常展示配置。
5. 使用临时 DB 和可用 sticky 代理跑一次纯协议注册 smoke test。
6. `docker build` 成功。
7. 容器内 WebUI 能启动，`/api/stats` 返回 200。

## 提交粒度

建议每个 PR 只覆盖一个 phase：

1. `refactor: introduce package skeleton`
2. `refactor: split sqlite repositories`
3. `refactor: unify mail and sms providers`
4. `refactor: split webui routers and services`
5. `refactor: split openai auth flow`
6. `chore: remove legacy compatibility modules`

不要在一个 PR 里同时拆数据库、WebUI 和 `auth_flow.py`。

## 不建议现在做的事

1. 不要一次性删除根目录兼容模块，外部脚本和测试仍在引用。
2. 不要先重写 `run_register()` 状态机，先做机械拆分。
3. 不要把 SQLite 换成 PostgreSQL/ORM，这是独立架构决策，不属于本轮结构重构。
4. 不要把 provider 逻辑塞进 API route，重构方向相反。
5. 不要在重构 PR 里顺带修改注册协议、代理策略或打码平台。
