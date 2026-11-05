# ─────────────────────────────────────────────────────────────────────────────
# gpt-outlook-register —— WebUI 的容器镜像
#
# 【关于体积：node 到底占多少】
#   怕"node 镜像占空间"多半是想到了 node:22（**1.1GB**，带完整 npm）。
#   但这里用不到那个：
#     · 前端构建 → node:22-alpine（~137MB），**多阶段构建，不进最终镜像**
#     · 运行时   → 只 COPY 一个 node 二进制（~110MB），不要 npm / node_modules
#                  （sentinel_quickjs.py 只执行 `node <script>`，不需要 npm）
#
# 【四阶段设计：每一步都在压体积】
#   1. web-build  node:22-alpine   构建 Vue 前端          → 产物进最终镜像
#   2. py-deps   python:3.13-slim  装 Python 依赖(含编译工具链) → 只进最终镜像的产物
#   3. runtime   python:3.13-slim  干净运行时 + node 二进制
#
#   关键：编译工具链（build-essential/gcc，约 150MB）只在 py-deps 阶段存在，
#   最终镜像里**一片 gcc 都不会有**。
#
#   最终镜像估算：
#     python:3.13-slim            ~130MB
#     + ca-certificates/curl 等     ~25MB
#     + Python 依赖 (site-packages)  ~60MB
#     + node 二进制                ~110MB
#     + 应用代码                    ~5MB
#     ─────────────────────────────
#                                  ~330MB   (docker save 后压缩约 140MB)
#
# 【为什么必须有 Node】
#   Sentinel token 用 QuickJS 方案：纯 Python 算的合成 PoW 只能骗过 OpenAI 的
#   浅层校验，OTP 邮件会被**静默丢弃**（表现为注册跑到最后一步收不到码）。
#   必须真在 JS VM 里跑一遍 OpenAI 的 sdk.js → 需要 `node <script>`。
#
# 构建：docker build -t gpt-outlook-register:local .
# 运行：docker compose up -d
# ─────────────────────────────────────────────────────────────────────────────

# ── 阶段 1：构建前端（Vue 3 + Vite，产物输出到 webui/static）──────────────
FROM node:22-alpine AS web-build

WORKDIR /app/webui/frontend

# 先只 COPY 依赖清单 —— 改源码时这一层能命中缓存，不用重装依赖
COPY webui/frontend/package.json webui/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# 再 COPY 源码并构建。vite.config.js 里 outDir='../static'，产物落在 /app/webui/static
COPY webui/frontend/ ./
RUN npm run build


# ── 阶段 2：装 Python 依赖（带编译工具链，装完就丢）──────────────────────
FROM python:3.13-slim AS py-deps

# build-essential / libssl-dev 只在这一阶段存在 —— 装带 C 扩展的包
# （curl_cffi 等）需要它们。产物 COPY 到运行阶段后，这些一个不留。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && find /usr/local/lib/python3.13/site-packages -name "*.a" -delete \
    && find /usr/local/lib/python3.13/site-packages -name "__pycache__" -type d -prune -exec rm -rf {} +


# ── 阶段 3：运行时（干净 Python + 极简 node）──────────────────────────────
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 运行时只留真正需要的：
#   - ca-certificates / curl：curl_cffi 走 HTTPS 要用
#   - git：主人的部署方式 —— 同步到 GitHub 后在服务器上拉代码 / 更新
#   - libstdc++6：node 二进制的动态依赖（node 官方 tarball 不自带）
#   - tzdata：时区
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        libstdc++6 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（从 py-deps 阶段，只拿产物，不拿编译工具链）
COPY --from=py-deps /usr/local/lib/python3.13/site-packages/ /usr/local/lib/python3.13/site-packages/
COPY --from=py-deps /usr/local/bin/ /usr/local/bin/

# node 二进制：只取这一个可执行文件，不要 npm / corepack / node_modules。
# node:22-slim 里 node 装在 /usr/local/bin/node（官方 tarball 的位置）。
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node

# ⚠️ 构建期就验证 node 能跑 —— 缺动态库（比如 libstdc++6）的话，
#    在 build 阶段就会报错，而不是等到跑注册时才发现 sentinel 静默失败。
RUN node -e "console.log('node ok:', process.version)"

WORKDIR /app

# 应用代码
COPY auth_flow.py config.py fingerprint.py http_client.py main.py ./
COPY mail_cf.py mail_outlook.py openai_sentinel_quickjs.js ./
COPY register_outlook.py sentinel.py sentinel_quickjs.py start_webui.py ./
COPY mail_providers/ ./mail_providers/

# webui 后端代码。
# ⚠️ 只 COPY 后端真正需要的部分 —— 整个 webui/ 目录里还压着
#    frontend/ 的源码和 node_modules，白白多几十 MB。
#    静态资源（static/）在下面从 web-build 阶段单独 COPY 进来
#    （.dockerignore 里排除了 webui/static/，本地那份不进镜像）。
COPY webui/*.py ./webui/

# 前端构建产物
COPY --from=web-build /app/webui/static/ ./webui/static/

# 数据目录：DB / 日志都通过环境变量指到这里，compose 里挂出去
RUN mkdir -p /app/data/logs

# WebUI 端口
EXPOSE 8765

# 健康检查：后端起来后 /api/stats 应该返回 JSON
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/stats', timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "webui.app:app", "--host", "0.0.0.0", "--port", "8765", "--access-log"]
