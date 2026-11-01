# 项目代码说明（中文）

本文根据当前仓库代码整理，说明 T470s 笔记本状态仪表盘的结构、执行流程和关键设计。

## 1. 项目概览

这是一个通过浏览器查看笔记本状态的 Web 应用，展示 CPU、内存、温度、电池、磁盘、网络、进程、定时任务和 OpenClaw 模型信息。

核心特点是 **按需采集，而不是持续监控**：页面打开时请求一次最新快照，短时间重复刷新复用缓存，没有后台定时采集、轮询或 WebSocket。

技术组成：

- 后端：FastAPI、Uvicorn。
- 模板：Jinja2。
- 系统信息：psutil、Linux 系统文件和命令行工具。
- 前端：原生 JavaScript、HTML、CSS。
- 持久化：JSON 快照文件，不依赖数据库。

## 2. 主要文件及职责

| 文件绝对路径 | 职责 |
| --- | --- |
| `/home/zengkai/Codes/laptop-status-dashboard/app.py` | Web 服务、登录认证、快照缓存和 API |
| `/home/zengkai/Codes/laptop-status-dashboard/collector.py` | 系统及硬件数据采集 |
| `/home/zengkai/Codes/laptop-status-dashboard/templates/index.html` | 仪表盘页面结构和初始快照容器 |
| `/home/zengkai/Codes/laptop-status-dashboard/templates/login.html` | 登录表单及错误提示 |
| `/home/zengkai/Codes/laptop-status-dashboard/static/dashboard.20261101.js` | 请求快照并更新页面 |
| `/home/zengkai/Codes/laptop-status-dashboard/static/dashboard.20261101.css` | 深色主题、卡片和页面布局 |
| `/home/zengkai/Codes/laptop-status-dashboard/data/snapshot.json` | 默认的最近一次快照存储位置 |
| `/home/zengkai/Codes/laptop-status-dashboard/tests/test_app.py` | 密码、登录、缓存、页面和 API 测试 |
| `/home/zengkai/Codes/laptop-status-dashboard/tests/test_collector.py` | 定时任务采集测试 |

快照目录可以通过环境变量修改，表中列出的是默认位置。

## 3. 一次页面访问的执行流程

```text
浏览器 GET /
    ↓
检查登录 Cookie
    ├─ 未登录：跳转 /login
    └─ 已登录：读取已有快照，将快照 JSON 嵌入 HTML
                    ↓
            JavaScript 展示旧快照
                    ↓
            POST /api/refresh
                    ↓
            检查缓存是否足够新
              ├─ 默认不足 10 秒：复用缓存
              └─ 已过期或无缓存：采集并保存新快照
                    ↓
            返回 JSON，更新页面各区域
```

这里需要区分：

- `GET /` 只读取缓存，不执行系统采集。
- `POST /api/refresh` 才负责复用缓存或触发采集。
- 首页返回的是包含初始快照 JSON 的 HTML 框架，具体指标由 JavaScript 填入，不是后端直接将所有指标渲染成 HTML。
- 首次运行没有缓存时，页面先显示占位内容，等待刷新接口返回数据。

## 4. 后端代码

代码位置：`/home/zengkai/Codes/laptop-status-dashboard/app.py`。

### 4.1 环境变量配置

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `DASHBOARD_DATA_DIR` | 快照数据目录 | 项目根目录下的数据目录 |
| `DASHBOARD_MIN_REFRESH_SECONDS` | 两次实际采集的最小间隔 | 10 秒 |
| `DASHBOARD_SESSION_TTL_SECONDS` | 会话有效期 | 7 天 |
| `DASHBOARD_COOKIE_SECURE` | Cookie 是否仅通过 HTTPS 发送 | 开启；设为 `0` 关闭 |
| `DASHBOARD_PASSWORD_HASH` | 访问密码的哈希 | 空字符串 |
| `DASHBOARD_SESSION_SECRET` | 会话签名密钥 | 空字符串 |

密码和签名密钥应通过环境配置提供，而不是直接写入源代码。

### 4.2 密码哈希与验证

`make_password_hash()` 使用随机盐和 PBKDF2-HMAC-SHA256 生成密码摘要，默认迭代 310,000 次。保存格式为：

```text
pbkdf2_sha256$迭代次数$随机盐$摘要
```

`verify_password()` 解析保存的参数，对用户输入执行相同计算，再通过 `hmac.compare_digest()` 比较结果。系统不需要保存明文密码。

密码不正确时，登录接口等待约 0.35 秒后返回 HTTP 401 和错误提示。

### 4.3 签名 Cookie 会话

登录成功后，服务器设置 `laptop_status_session` Cookie，内容形式为：

```text
v1.过期时间.签名
```

`_sign_session()` 使用 `SESSION_SECRET` 计算 HMAC-SHA256 签名；`_valid_session()` 检查格式、版本、过期时间及签名。

这是无服务端会话表的签名 Cookie 方案，而不是数据库会话。Cookie 设置了：

- `HttpOnly`：禁止页面 JavaScript 直接读取。
- `Secure`：默认仅通过 HTTPS 发送。
- `SameSite=Strict`：限制跨站请求携带 Cookie。

`POST /logout` 删除当前浏览器的 Cookie，但代码没有服务端撤销列表。因此，删除浏览器 Cookie 不等于让已复制出去的同一令牌立即失效。

### 4.4 SnapshotStore：快照缓存

`SnapshotStore` 管理快照读取、有效期判断、采集和保存。

#### 读取

`load()` 读取 JSON 文件。如果文件无法读取、JSON 损坏或顶层数据不是字典，则返回 `None`。

#### 复用近期缓存

`_age_seconds()` 根据 `collected_at` 计算快照年龄。`refresh()` 在年龄小于最小刷新间隔时返回缓存，并将来源标记为 `recent-cache`。

#### 串行刷新

刷新逻辑放在 `threading.Lock` 内，避免同一进程中的多个请求同时采集和写文件。该锁不是跨进程锁，部署配置因此也采用单个 Uvicorn worker。

#### 原子替换

新快照先写入临时文件，再通过 `os.replace()` 替换正式文件，避免读请求获取只写了一半的 JSON。

### 4.5 路由与失败回退

| 路由 | 行为 |
| --- | --- |
| `GET /healthz` | 返回简单健康状态，不要求登录 |
| `GET /login` | 显示登录页；已登录则跳转首页 |
| `POST /login` | 验证密码并设置 Cookie |
| `POST /logout` | 删除 Cookie 并跳转登录页 |
| `GET /` | 验证会话并返回包含缓存快照的页面 |
| `POST /api/refresh` | 验证会话，复用缓存或执行采集 |

刷新接口通过 `run_in_threadpool(store.refresh)` 执行同步采集，避免将文件读取和命令执行等阻塞操作直接放在异步事件循环中。

接口中的 `source` 有三种主要取值：

| source | 含义 |
| --- | --- |
| `fresh` | 本次实际完成采集 |
| `recent-cache` | 快照足够新，直接复用 |
| `stale-fallback` | 本次刷新失败，返回已有旧快照 |

如果整个刷新失败且没有旧快照，接口返回 HTTP 503。未登录调用刷新接口则返回 HTTP 401。

### 4.6 安全和缓存响应头

中间件设置内容类型保护、禁止页面被嵌入框架、内容安全策略等响应头。

- 静态资源设置一年缓存和 `immutable`。
- 除健康检查外，其他响应设置 `no-store`。
- `_safe_json()` 将 JSON 中的 `<` 转义，避免初始快照中的文本提前结束 HTML 内的 `<script>` 容器。

## 5. 数据采集器

代码位置：`/home/zengkai/Codes/laptop-status-dashboard/collector.py`。

### 5.1 数据来源

| 函数 | 采集内容 |
| --- | --- |
| `collect_system()` | 主机名、设备、系统、内核、运行时长、负载、进程和线程数量 |
| `collect_cpu()` | CPU 型号、核心数量、使用率、各核心频率、电源策略 |
| `collect_temperatures()` | Linux thermal zone 温度 |
| `collect_fan()` | ThinkPad 风扇转速、级别和状态 |
| `collect_memory()` | RAM 和 Swap 容量及使用率 |
| `collect_disks()` | 支持的文件系统分区容量和使用率 |
| `collect_processes()` | CPU 和内存占用较高的进程 |
| `collect_network()` | 网卡 IPv4、默认网关、外网检查和本地代理端口状态 |
| `collect_batteries()` | 各电池电量、容量和健康度 |
| `collect_schedule()` | 用户 Cron、用户 systemd timer、OpenClaw 自动任务和系统 timer 数量 |
| `collect_model()` | 活跃主会话记录的模型、提供者以及默认模型和回退模型配置 |

`_run()` 对命令执行设置超时，命令不存在、超时或返回非零状态时返回空字符串。`_read()` 对文件读取错误提供默认值。

采集主要读取状态，但不是完全没有网络活动：外网连通性检查会发送 HTTP 请求，代理端口检查会建立本地 TCP 连接。

### 5.2 先采 CPU，再并发采集其他模块

`collect_snapshot()` 首先执行 `collect_cpu()`，之后使用最多 8 个线程并发采集其余模块。

提前测量 CPU 的目的是避免后续较重的命令行采集操作抬高 CPU 使用率，让空闲机器看起来很忙。并发执行其他独立模块则可以缩短整体等待时间。

最终快照除业务模块外，还包含：

- `collected_at`：采集完成时间。
- `collection_ms`：整体采集耗时。
- `errors`：失败模块及异常类型。
- `summary`：整体健康提示。

### 5.3 单模块异常隔离

线程池任务分别捕获异常。一个模块失败后，记录异常类型并为该模块填充空列表或空字典，其他模块仍可返回。

需要注意：CPU 采集发生在线程池任务的异常隔离之外。如果 CPU 采集直接抛出异常，会由刷新接口进行整体失败回退。

### 5.4 电池容量与健康度

电池健康度计算公式为：

```text
单块电池健康度 = 当前满充容量 ÷ 设计容量 × 100%
```

例如设计容量为 24 Wh，当前满充容量为 18 Wh，健康度为 75%。

综合健康度采用容量加权方式：

```text
综合健康度 = 所有电池满充容量之和 ÷ 所有电池设计容量之和 × 100%
```

它不是两块电池健康百分比的简单平均值。

对于没有直接提供能量字段的电池，`_energy_wh()` 尝试使用电荷量和电压换算容量。

### 5.5 定时任务与模型信息

`collect_schedule()` 合并三类用户任务：

1. 当前用户的 `crontab`。
2. 用户级 systemd timer。
3. OpenClaw 自动任务。

系统级 timer 只报告数量。返回的任务数据不包含完整命令行，避免把路径或命令中可能存在的凭据直接展示到页面。

当前 Cron 解析逻辑以五个时间字段加命令的常规表达式为主，不是完整的 Cron 语法解析器。

`collect_model()` 从 OpenClaw 活跃会话中选择最近更新的主代理会话，并读取本地配置中的默认模型和回退模型。页面的“当前模型”反映的是这些记录，而不是实时探测某个模型服务正在执行请求。

### 5.6 健康摘要规则和局限

`_summary()` 使用以下固定阈值：

| 条件 | 提示 |
| --- | --- |
| 最高温度达到 75°C、未达到 85°C | 温度偏高 |
| 最高温度达到 85°C | 温度过高 |
| RAM 使用率达到 90% | 内存压力较高 |
| 任一采集分区使用率达到 90% | 磁盘空间紧张 |
| 综合电池健康度低于 60% | 电池综合健康度偏低 |
| 外网连通性检查未成功 | 外网连通性异常 |

有任一提示则为 `warning`，否则为 `healthy`。

这些规则不是完整诊断系统：

- 正常提示文案提到了“负载”，但实际没有 CPU 使用率或系统负载告警阈值。
- 外网状态依赖一个指定检测地址，检查失败不一定代表所有外网均不可用。
- 本地代理只检查端口能否建立连接，不验证是否能正常转发流量。
- 采集模块失败时，摘要可能基于缺失数据继续计算，需要结合 `errors` 理解结果。

## 6. 前端代码

主要逻辑位置：`/home/zengkai/Codes/laptop-status-dashboard/static/dashboard.20261101.js`。

### 6.1 初始快照

首页模板将快照放在 `type="application/json"` 的 `initial-snapshot` 元素中。JavaScript 读取其 `textContent`，解析后调用 `renderSnapshot()` 展示缓存数据。

这样可以先提供上次结果，再等待新的采集请求完成。

### 6.2 分模块渲染

`renderSnapshot()` 统一调用摘要、核心指标、电池、网络、硬件、模型、磁盘、进程和定时任务的渲染函数。

常用工具函数包括：

- `byId()`：按 ID 获取元素。
- `node()`：创建元素，并通过 `textContent` 写入文本。
- `setText()`：设置文字和缺失值占位符。
- `setMeter()`：设置进度条宽度。
- `clamp()`：将数值限制在指定范围。
- `formatCollectedAt()`：将时间格式化为中文，并统一使用上海时区。

主要通过 `textContent` 填充采集结果，而不是将结果直接拼接成 HTML，可以减少把普通数据误当作 HTML 解释的风险。

### 6.3 单次刷新和错误提示

脚本初始化结束后调用一次 `refreshOnce()`，发送 `POST /api/refresh`，然后根据返回结果更新页面：

- HTTP 401：跳转登录页。
- 新采集完成：展示完成提示。
- 复用近期缓存：展示缓存提示。
- 采集失败回退：保留旧数据并展示警告。
- 请求失败：不清空已有内容，提示稍后重试。

采集期间刷新按钮禁用，请求结束后恢复。

“刷新状态”按钮执行的是 `window.location.reload()`，即重新加载页面，再走一遍初始快照和单次刷新流程。代码没有定时轮询。

## 7. 部署结构

服务配置位置：`/home/zengkai/Codes/laptop-status-dashboard/ops/laptop-status-dashboard.service`。

它使用 Uvicorn 启动应用，监听笔记本本地 `127.0.0.1:8765`，使用一个 worker，从环境文件加载配置，并在进程失败后自动重启。

仓库中的部署配置描述了以下链路：

```text
浏览器
  → 公网 Nginx（HTTPS）
  → 服务器本地 8081 端口
  → FRP 隧道
  → 笔记本本地 8765 端口
  → FastAPI
```

相关文件：

- `/home/zengkai/Codes/laptop-status-dashboard/ops/frpc-status-snippet.toml`
- `/home/zengkai/Codes/laptop-status-dashboard/ops/nginx-status-dashboard-upstream.conf`
- `/home/zengkai/Codes/laptop-status-dashboard/ops/nginx-status.mitoto.cn.conf`

以上说明来自仓库配置，不代表已经验证线上环境当前的实际运行情况。

## 8. 测试与验证范围

整理本文前运行了现有测试，结果为：

```text
6 passed, 1 warning
```

测试覆盖密码哈希验证、近期缓存复用、首页不触发采集、登录及刷新 API、静态资源缓存策略、定时任务来源合并和缺失来源容错等行为。

警告来自测试依赖的弃用提示，没有测试失败。以上测试不等同于真实硬件采集验证、浏览器端到端验证或线上部署验证。

## 9. 总结

本项目的核心是 **按需采集 + JSON 快照缓存 + 前端局部更新**。后端负责认证、采集协调和持久化，采集器负责提供结构化数据，前端负责展示缓存并在单次刷新后更新页面，从而避免持续后台采集带来的开销。
