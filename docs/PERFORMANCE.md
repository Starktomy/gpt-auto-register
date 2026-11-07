# 注册性能与随机节奏基准

更新日期：2026-11-07

本文记录真实注册链路的耗时结构、已经落地的优化、现有随机间隔，以及后续不应轻易绕过的保护逻辑。单次真实注册受邮箱到达时间、OpenAI 网络延迟、服务端 challenge 要求影响，不能把一次总耗时当作严格 A/B 结论。

## 真实样本

| Run | 总耗时 | 该样本的主要状态 |
| --- | ---: | --- |
| `11d929d249dd` | 71.2s | 历史成功基线，Node 输出后仍被 SDK 定时器挂住 |
| `959a509df691` | 48.3s | Node 输出 flush 后立即退出 |
| `7b99786777c0` | 36.0s | 加入 Sentinel 分段计时、总时长抖动和计时日志 |
| `85718e2f336e` | 31.6s | SO 未要求时跳过行为采集；Graph/IMAP 双通道竞速 |

历史基线之后的样本均开启 TOTP，并关闭 Codex refresh token 交换，使数据更接近注册、OTP、Sentinel、建号和 TOTP 主链路。

## Sentinel

### Node 挂起修复

最初的主要瓶颈不是 PoW 哈希循环，而是 Node 写完 JSON 后没有退出，SDK 留下的定时器会把进程继续挂到超时。现在包装器在 stdout/stderr flush 完成后立即调用 `process.exit()`。

本地受控基准：

| 指标 | 修复前 | 修复后 |
| --- | ---: | ---: |
| `requirements` | 约 8150ms | 约 140-150ms |

该动作约提升 56 倍，消除的是无功能的进程等待，不改变 SDK 输出。

### 分段计时

Sentinel 成功日志现在包含：

```text
sdk=<SDK 缓存/下载耗时>
requirements=<requirements 动作耗时>
challenge=<POST /sentinel/req 耗时>
solve=<完整 solve 动作耗时>
behavior=<本次行为采集时长>
```

`7b99786777c0` 的三次 Sentinel：

| 指标 | 数值 |
| --- | ---: |
| requirements | 每次 142-147ms |
| challenge 网络 | 每次 161-521ms |
| solve | 4313-4495ms |
| 行为采集 | 4110-4312ms |
| Sentinel 总计 | 14.45s |
| 行为采集总计 | 12.55s |
| 非行为部分总计 | 1.90s |

`85718e2f336e` 应用 SO 条件跳过后：

| Sentinel | requirements | challenge | solve | behavior |
| --- | ---: | ---: | ---: | ---: |
| 需要 SO | 147ms | 187ms | 3435ms | 3244ms |
| 不需要 SO | 144ms | 141ms | 174ms | 0ms |
| 需要 SO | 163ms | 190ms | 3678ms | 3478ms |

该 run 的 Sentinel 总耗时降到 8.26s，其中行为采集 6.72s，非行为部分 1.54s。

关键规则：只有服务端要求 Session Observer token 时才执行行为采集。`challenge.so.required` 不是 `true` 时，`behavior_duration_ms` 设为 0。主 Sentinel token 在行为序列开始前已经生成，因此该跳过不会削弱主 token。

### 行为节奏

默认行为基础时长为 3400ms，上下抖动 250ms，且只在需要 SO 时应用。事件序列本身已有随机性：

| 事件 | 随机范围 |
| --- | --- |
| pointer move 数量 | 12-16 次 |
| pointer move 间隔 | 70-145ms |
| 点击前停顿 | 90-220ms |
| scroll 数量 | 3-4 次 |
| scroll 间隔 | 80-180ms |
| wheel 前停顿 | 80-160ms |
| 每个 key 前停顿 | 90-210ms |

去掉总时长兜底后，自然事件序列实测约 2762-3229ms。当前 3400ms 仍给完整事件序列留有余量。没有至少 5 个真实成功样本支持时，不要继续降低默认值。

本地性能测试可以显式关闭：

```bash
OPENAI_SENTINEL_BEHAVIOR_MS=0
OPENAI_SENTINEL_BEHAVIOR_JITTER_MS=0
```

这两个值只用于本地基准。生产注册如果需要 SO token，禁止设置为 0。

## Outlook OTP 双通道

旧实现先跑 Graph，Graph 失败后才启动 IMAP。当前号池的 Graph token 交换稳定失败，原因是 client 没有 Graph 邮件权限，这会让 IMAP 晚约 1 秒启动。

Graph 没有被跳过。现在 `wait_for_otp()` 同时启动 Graph 和 IMAP：

1. 两个通道使用相同时间窗和 deadline。
2. 任一通道先拿到 OTP 即获胜。
3. 获胜通道设置停止事件。
4. 失败通道在下一个安全轮询边界退出。
5. 单通道失败时，另一通道继续等待。
6. 双通道都失败时，以 IMAP 错误作为主错误抛出。

两个通道独立使用 3.0-4.5 秒随机轮询间隔。`85718e2f336e` 中 Graph 约 1 秒失败，IMAP 约 9 秒拿到 OTP。该样本总 OTP 时间没有明显下降，因为邮件到达时间占主导；但 IMAP 现在立即启动，不再等待 Graph 失败。

后续可研究 IMAP IDLE 或复用 IMAP 连接，但风险高于双通道竞速，需要谨慎处理 Outlook 认证、文件夹发现和连接生命周期。

## TOTP 复核

TOTP enroll 和 activate 接口返回 200 已经是权威成功信号。旧代码之后固定 sleep 2 秒再查 `mfa_info`。

当前行为：

1. activate 成功后立即查询 `mfa_info`。
2. 如果启用状态尚未传播，等 0.4 秒重试。
3. 仍未传播，再等 1.0 秒重试。

该优化在 `85718e2f336e` 之后落地，已有单元测试覆盖，但尚未用下一次真实注册重新测量。常见路径预计节省 1-2 秒。

## 剩余耗时

以 `85718e2f336e` 的 31.6 秒样本估算：

| 阶段 | 约耗时 | 判断 |
| --- | ---: | --- |
| 网络检查、warmup、CSRF、OAuth 初始化 | 1s | 不建议删除；用于降低 403/409 |
| 第一次 Sentinel 和 signup 切换 | 4s | 主要是服务端要求 SO 的行为采集 |
| 密码注册和无需 SO 的 Sentinel | 2s | 已优化 |
| OTP 派发与收取 | 9s | 主要受邮件到达时间影响 |
| 建号和第三次 Sentinel | 7s | SO 行为加 OpenAI 网络 |
| redirect 和 auth session | 3s | 状态机与网络依赖 |
| TOTP 绑定与复核 | 4s | 短轮询优化应继续降低 |
| 套餐查询和完成 | 1s | 可后台化 |

## 后续候选

按优先级排序：

1. 将套餐查询移到 `done` 事件之后。账号在套餐查询前已经可用，前端可以先收 `done`，之后再收 `plan_checked`。
2. 当 refresh token 和 Codex OAuth 都关闭时，跳过无条件的 `client_auth_session_dump` 请求。该路径当前返回 404，可省一次小网络请求。
3. 在 3400ms 默认值累计 5 个真实成功样本后，再测试 `OPENAI_SENTINEL_BEHAVIOR_MS=3200`。
4. 将自动跑号固定 3 秒冷却改成 2.5-4.5 秒随机冷却。
5. 给失败路径退避加约 ±20% 抖动，例如 warmup、CSRF 403 和 invalid-state 重试。这不会加快成功路径，但能降低并发 worker 的同步特征。

不要做以下优化：

- 不要用纯 Python synthetic PoW 替代真实 Sentinel SDK；深层校验会拒绝。
- 不要跨 flow 或跨 challenge 复用 `request_p`。
- 不要为省每次约 140ms 的 Node 启动而把一个 VM 复用给多个注册；SDK 状态和指纹隔离更重要。
- 不要为省约 1 秒删除代理检查或 warmup。
- 生产环境需要 SO 时，不要关闭行为采集。

## 基准流程

可比样本使用：

```bash
regix status
regix register --totp --no-refresh-token
regix runs --limit 10
```

修改默认节奏前至少收集 5 个真实成功样本，并记录：

- run 总耗时
- 每次 Sentinel 的分段耗时
- 需要 SO 的 Sentinel 次数
- OTP 获胜通道和耗时
- TOTP activate 到复核完成的耗时
- 失败类别，如有

性能文档和日志不得包含密码、TOTP secret、token、refresh token 或完整邮箱凭证。
