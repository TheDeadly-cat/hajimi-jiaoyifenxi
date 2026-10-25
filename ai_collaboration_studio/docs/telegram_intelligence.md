# Telegram 只读情报采集与交付

## 当前边界

这是一条独立于 Studio 正式业务库的 Telegram 只读来源管线。第一阶段只接入
Telegram/TDLib，不接入 Grok，也不提供下单、交易、钱包、加群、转发消息或修改群权限的
能力。Telegram 文本、链接、转发来源和媒体元数据一律视为不可信数据，不能成为系统指令。

当前实现由本地单元测试和静态交付合同覆盖；本次交付**没有**加载真实 TDLib 动态库、登录
真实 Telegram 账号、拉取真实聊天、调用真实 OpenAI、发送真实 Bot 消息或构建/运行 Docker
镜像。因此这些能力不能被描述为已经完成真实集成验证。

宿主策略在 `backend.config.HARD_DISABLED_PROVIDER_IDS` 中硬禁用 OpenAI。Telegram 分析器在
Provider registry 拒绝 OpenAI 时会关闭失败；注入的 Fake/测试 Provider 只用于离线测试。
本文不提供绕过宿主策略的方法。任何未来真实 OpenAI 启用都必须经过独立、明确的授权与
宿主治理变更；拥有 API key 本身不构成授权，也不要仅为本功能填写 OpenAI key。

## 已实现的组件与数据流

1. `tdlib.py` 用 `ctypes` 加载 TDLib JSON 动态库，管理请求关联、更新队列和交互式授权。
2. `config.py` 严格解析聊天白名单；未列出或 `enabled: false` 的 chat 在深处理前即被忽略。
3. `normalizer.py` 将新消息、编辑和删除更新投影为 UTC、类型化的来源记录。
4. `dedup.py`、`rules.py`、`scoring.py` 和 `events.py` 在本地做去重、关键词/受控 ticker
   匹配、预评分、事件聚类与来源身份计数。不同 Telegram 身份不等于组织上独立的来源，当前
   评分不会据此授予“独立核验”加分。
5. `repository.py` 使用专属 SQLite/WAL 保存消息修订、事件、分析结果、告警、用量、缓存和
   持久重试队列；它明确拒绝 Studio 正式数据库路径及
   `collaboration_studio.sqlite3` 文件名。
6. `analyzer.py` 只接受结构化输出，把 Telegram 内容标为不可信，并限制核验状态与动作枚举。
   未显式配置价格时只记录 token、model、request ID 和 latency，不声称精确费用。
7. `alerts.py` 对普通重复事件做冷却；方向变化可触发更新。“官方否认”旁路只接受未来由宿主
   确定性核验器设置的原生布尔值，模型的事件类型或风险标签不能触发它。
8. `bot.py` 只能向启动时固定的目的地输出，转义 HTML 并显示证据状态；格式器支持
   `RUMOR`/`CONFIRMED` 等标签，但当前 Telegram-only 管线会把模型声称的 `confirmed` 降级为
   `likely`。真实发送请求必须经过 Studio `execution_boundary.py` 的固定 `sendMessage` 构造器，
   不能选择任意 Bot 方法或动态目的地；`DRY_RUN=true` 只返回 `WOULD_SEND`，不调用 Bot API。
9. `health.py` 提供不含消息内容的 `/health` 和 `/status`，并强制只绑定 loopback。
10. `cli.py` 提供登录、聊天发现、运行、有限历史补采和本地状态命令。
11. `pipeline.py` 编排持久化、规则、去重、聚类、AI 分析、评分、冷却与告警工作；
    外部调用不在 TDLib 接收回调里执行。
12. `runtime.py` 组装采集器、持久工作器、健康服务和有界历史补采，并将运行状态写入
    专属状态表。
13. `source_inbox.py` 把已持久化事件投影为 Studio Source Inbox 合同；它是本地、手动调用的
    读取/投影边界，不会自动写入 Studio 正式数据库。

实时 TDLib 更新队列位于进程内；需要重试的工作进入同一专属 SQLite 的 `retry_jobs` 表，状态为
`PENDING`、`CLAIMED`、`RETRY`、`COMPLETED` 或 `DEAD`。这条队列和数据库都不复用 Studio
HTTP 服务、正式 SQLite 或现有房间运行时。

## 先决条件

- 使用与 Python 进程相同架构的官方 TDLib JSON 动态库。只从
  [TDLib 官方仓库](https://github.com/tdlib/td)及其构建说明获取或自行构建，不使用来源不明的
  预编译二进制。
- 在 [Telegram 官方应用管理页](https://my.telegram.org/apps)为自己的账号取得 `api_id` 和
  `api_hash`。二者与手机号、登录会话、2FA 密码、Bot token 都属于敏感信息。
- 使用独立的低权限 Telegram 账号更容易限制影响面；只白名单确有权读取的 chat。
- Python 入口必须从项目根目录运行。Telegram 模块本身只使用标准库；TDLib 是外部本机/容器
  动态库，不是 Python wheel。

### Windows：`tdjson.dll`

1. 按官方 Windows 构建说明生成或取得匹配 Python 位数的 `tdjson.dll`。
2. 将 `tdjson.dll` 及官方构建产生的依赖 DLL 放在一个受限的本机目录，不放进源码、
   `runtime` 备份或 Git。
3. 设置绝对路径；不要把整个不受信目录加入全局 `PATH`：

```powershell
$env:TELEGRAM_TDLIB_LIBRARY_PATH = 'C:\approved\tdlib\tdjson.dll'
```

若加载失败，先核对 x64/ARM64/32 位是否与 Python 一致，再用 Windows 的受信二进制检查工具
确认缺失依赖。不要通过下载随机 DLL 来“补齐”。

### Linux：`libtdjson.so`

按官方 Linux 构建说明准备 `libtdjson.so`，限制文件写权限，并在启动前用 `file` 和 `ldd`
确认架构及动态依赖。直接运行时设置：

```bash
export TELEGRAM_TDLIB_LIBRARY_PATH=/opt/tdlib/lib/libtdjson.so
```

容器镜像故意不捆绑 TDLib；Compose 只把用户提供的 Linux `libtdjson.so` 只读挂载到该路径。
如果 `ldd` 显示依赖缺失，应在经过审查的派生镜像中安装对应发行版包，不能把宿主 DLL、
会话目录或未知二进制复制进镜像。

## 本机配置

把 `.env.example` 的 Telegram 区块复制到未跟踪的本机 `.env.local`，只填写必要字段。
不要把 `.env.local`、手机号、API hash、Bot token、登录码或 2FA 密码提交、截图或写入日志。
安全默认值是：

```dotenv
ENABLE_AI=false
ENABLE_ALERTS=false
DOWNLOAD_MEDIA=false
DRY_RUN=true
TIMEZONE=Asia/Shanghai
```

默认运行数据位置如下：

| 数据 | 默认位置 | 说明 |
| --- | --- | --- |
| TDLib 会话/数据库 | `runtime/telegram/tdlib-data/` | 含可复用登录态；只有配置专用加密密钥时才是应用层加密 |
| TDLib 文件 | `runtime/telegram/files/` | 默认不下载媒体；仍按敏感数据保护 |
| 专属 SQLite | `runtime/telegram/telegram.sqlite3` | WAL/SHM 与主文件属于同一恢复单元 |
| chat 配置 | `config/chats.yaml` | 含 chat ID/名称，可能敏感 |
| keyword 配置 | `config/keywords.yaml` | 本地确定性规则 |

`TELEGRAM_MAX_DATABASE_BYTES` 默认 `4294967296`（4 GiB，可配置范围 128 MiB–1 TiB）。主库、
WAL 与 SHM 的合计占用达到 90% 时 `/health` 降级为 warning，同时拒绝新的消息/任务准入并
暂停 worker 领取现有 backlog；剩余 10% 是单个已在途工作、SQLite 记账和干净关停的保留空间。
达到 100% 会显示 full，但不应把 100% 当作正常停写目标。容量拒绝使用
`TelegramStorageCapacityError`，不会自动删除消息、修订、分析或幂等记录，也不是归档/保留
策略；进入 warning 时应先停止服务、把 SQLite 三件套作为整体备份并制定可审计的归档/保留
方案，再提高容量或恢复采集。

仓库只提供 `.example.yaml`，且 `.gitignore` 已明确排除 `config/chats.yaml` 与
`config/keywords.yaml`。更安全的做法仍是把两份正式 YAML 放在
`runtime/telegram/config/` 或其他受限目录，并通过
`TELEGRAM_CHATS_CONFIG`、`TELEGRAM_KEYWORDS_CONFIG` 指向它们。不要修改示例文件来存放真实
chat ID。

### 聊天白名单

`config/chats.example.yaml` 展示所有允许字段：

- `chat_id`：从 `telegram chats` 输出复制的非零 64 位整数；supergroup/channel 通常为负数。
- `enabled`：必须显式设为 `true` 才采集；新条目保持 `false`。
- `name`、`category`：本地标签，不改变 Telegram 状态。
- `priority`：仅允许 `low`、`medium`、`high`。
- `trust_score`：0–100 的人工来源先验，不等于事实已经确认。

解析器最大接受 64 KiB、500 条 chat policy，并拒绝未知字段、重复 ID、重复键、YAML anchor、
tag 和复杂值。严格失败是预期的安全行为。

### Bot 告警与固定目的地

Bot 仅用于输出，不参与采集。先在 Telegram 官方 `@BotFather` 创建专用 Bot，只授予目标
私聊/群组/频道所需的最小发送权限。把 token 直接存入未跟踪的 `.env.local` 或受控环境变量
`TELEGRAM_BOT_TOKEN`；不要把它粘贴进 YAML、源码、截图、文档、聊天或共享 shell 历史。

先在目标会话中建立可见会话，再使用本项目的只读
`python -m backend.telegram_intelligence telegram chats` 输出核对 chat ID，或使用 Telegram
官方 Bot API 的 `getUpdates` 通过本机、能保护秘密的工具核对。不要把 token 放在命令行参数、
URL、日志或本文示例中。把核对后的**非零数字 chat ID** 写入
`TELEGRAM_DESTINATION_CHAT_ID`；不接受可被重新分配的 `@username`。目的地在进程启动时固定，
其指纹与告警工作绑定，不从消息内容、AI 输出或远程请求动态选择。若重启后目的地变更，旧工作
会被隔离而不是静默改投新会话。

首先保持 `ENABLE_ALERTS=true` 且 `DRY_RUN=true` 审核 `WOULD_SEND` 格式。只有在目的地、Bot
权限、脱敏日志和运营授权均经独立核对后，才能考虑 `DRY_RUN=false`。发送过的消息不会
被本管线自动撤回或删除。

### 关键词与 ticker

`config/keywords.example.yaml` 的稳定顶层合同是：

- `groups:` 下每个分类包含 `keywords`、`regex`、`tickers` 三个嵌套列表；示例把未使用的
  `regex`/`tickers` 显式写为 `[]`；
- `ticker_whitelist:` 下每个 ticker 对应公司别名列表；
- `ambiguous_tickers:` 列出必须有明确 ticker 表示或受控别名才可匹配的符号；
- `ticker_context:` 为普通 token 匹配提供本地语境；
- `breaking_keywords:` 是突发信号列表，不替代来源核验。

关键词命中、ticker 命中、优先级和来源信任只影响本地预评分，不会把传闻自动升级为确认。
模型建议的 ticker 只保留为审计字段；只有已经在本地白名单中且正文被确定性规则命中的 ticker
才能进入事件与告警。自定义 regex 禁止 lookaround、反向引用和所有无界 `*`、`+`、`{m,}`
重复；请使用有界重复。

## CLI 操作

所有本机 Telegram CLI 都使用同一前缀：

```powershell
python -m backend.telegram_intelligence telegram --help
```

### 1. 登录与 2FA

先在当前进程环境或 `.env.local` 设置 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、
`TELEGRAM_PHONE` 和 TDLib 绝对路径，然后运行：

```powershell
python -m backend.telegram_intelligence telegram login --timeout 180
```

登录码和已启用账号的 2FA 密码由隐藏输入提示读取，不写入配置。成功后授权状态保存在
`TELEGRAM_TDLIB_DATABASE_DIR`；后续运行通常复用该会话。可通过未跟踪的
`TELEGRAM_TDLIB_DATABASE_ENCRYPTION_KEY` 配置 TDLib 本地数据库加密；该值一旦用于现有会话
就必须安全保留，否则会话库无法重新打开。未配置时不能声称数据库已加密，只能依赖本机 ACL、
磁盘加密和目录隔离。不要把密钥或会话目录复制给其他人。

### 2. 查找 chat ID 并建立白名单

```powershell
python -m backend.telegram_intelligence telegram chats --limit 100 --timeout 30
```

只把输出中核对过的 `chat_id` 写入私有 `chats.yaml`。列出 chat 不会自动把它加入白名单；
采集器也没有加入群、转发消息或修改权限的方法。

### 3. 运行只读服务

保持 `DRY_RUN=true` 完成首轮观察：

```powershell
python -m backend.telegram_intelligence telegram run
```

`ENABLE_ALERTS=false` 时不输出告警；如仅验证格式，可在经过本机测试授权后启用 alerts 但继续
保持 `DRY_RUN=true`，结果应记录为 `WOULD_SEND`，不会调用 Telegram Bot API。

在 Windows PowerShell 和 Linux shell 中都应从项目根目录执行同一入口。前台运行时用
`Ctrl+C` 发送 `SIGINT` 请求优雅停止，并等待进程完全退出后再备份 SQLite、替换配置或移动
TDLib 会话目录。正常关停会先退出采集循环、关闭 TDLib 接收端以冻结有界更新队列，再让队列中
已经接收的更新走原有白名单与持久化入口，最后停止异步 worker；退出结果中的
`drained_updates` 是这一步实际处理的更新数。`TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS` 默认 90 秒；
只有 worker 已实际退出才返回 `status=stopped`。超过期限会返回错误，非 daemon worker 仍会
等待当前有界外部调用结束并写回持久状态，操作员可继续等待或明确选择强制终止并接受不确定交付：

```powershell
# Windows PowerShell（启动）
python -m backend.telegram_intelligence telegram run
# 停止：在同一终端按 Ctrl+C，等待命令返回。
```

```bash
# Linux（启动）
python -m backend.telegram_intelligence telegram run
# 停止：前台按 Ctrl+C，或让受管服务向进程发送 SIGINT并等待退出。
```

不要把强制终止或删除运行目录作为正常停止流程。Docker Compose 的 `stop_grace_period` 默认
105 秒，用于覆盖默认 90 秒 worker 期限及 TDLib 清理。`SIGKILL`、任务管理器强杀、进程崩溃、队列
溢出或停机排空时命中存储容量门，仍可能落入下文记录的采集恢复空档，应执行有界历史补采。

此阶段没有独立外部事实核验源。即使模型返回 `verification_status=confirmed`，管线也会保留
模型原始声称用于审计，但把运营状态降级为 `likely`；Bot 显示为 `LIKELY / NOT CONFIRMED`。
单个 Telegram 帖子、转发、截图、强烈措辞或模型分类都不是外部核验。

### 4. 有界历史补采

默认只取 100 条，并且 chat 必须已在白名单中启用：

```powershell
python -m backend.telegram_intelligence telegram history --chat-id -1000000000001
python -m backend.telegram_intelligence telegram history --chat-id -1000000000001 --limit 100
python -m backend.telegram_intelligence telegram history --chat-id -1000000000001 --limit 100 --since-time 2026-08-29T00:00:00+08:00
```

`--since-time` 必须带时区并在内部转换为 UTC。`TELEGRAM_HISTORY_MAX_LIMIT` 默认 1000，配置
允许范围为 100–10000；单次 `--limit` 必须在 1 与该上限之间。历史分页每页最多 100，仍走
与实时消息相同的白名单、标准化和幂等存储路径。

### 5. 健康状态

```powershell
python -m backend.telegram_intelligence telegram status --timeout 2
```

默认查询 `http://127.0.0.1:8781/status`。`/health` 只给 readiness 与 uptime，`/status`
还包含 TDLib/授权/网络/AI/Bot 状态、队列/重试/dead-letter 数、收取/处理计数与最近时间；不含
消息文本、手机号、token 或文件路径。`storage_status`、`database_bytes` 与
`database_limit_bytes` 用于观察专属 SQLite 水位；warning/full 都会使 readiness 降级。非
loopback 地址会在启动前失败。

命令成功返回 0；参数错误返回 2；配置、授权、TDLib 或运行错误返回 1。CLI 用一行 JSON 输出，
已知敏感字段被过滤，未知异常只返回通用错误。不要因为错误输出经过过滤就把完整终端日志公开。

## UTC 与 Asia/Shanghai

Telegram Unix 时间、消息/编辑/删除时间、事件时间、告警时间、AI 用量时间和重试租约都以
timezone-aware UTC 规范化并持久化，ISO-8601 输出使用 `Z`。`TIMEZONE=Asia/Shanghai` 是默认
运营/展示时区约定，不改变数据库的 UTC 事实时间。人工输入时间必须携带 `Z` 或显式偏移；
不要把无时区的本地时间当成 UTC。

## Docker（未做构建或运行验证）

`Dockerfile.telegram` 和 `docker-compose.telegram.yml` 是最小交付合同，不是已验证镜像。
构建上下文固定为 `backend/`，并由 `backend/.dockerignore` 默认拒绝、只放行 Telegram
运行所需 Python 源码。镜像不包含 TDLib、`.env`、密钥、宿主登录会话、SQLite/WAL/SHM、
图片或下载媒体。

Compose 要求两个只读 bind mount：一个私有配置目录和一个用户提供的 Linux
`libtdjson.so`；运行数据只写入 Docker named volume。它不声明 `ports`，健康端点只在容器
loopback 内可见。先在宿主环境设置路径和 Telegram 凭据，再执行：

```powershell
$env:TELEGRAM_CONFIG_DIR_HOST = 'C:\private\telegram-config'
$env:TELEGRAM_TDLIB_LIBRARY_PATH_HOST = 'C:\approved\tdlib-linux\libtdjson.so'
docker compose -f docker-compose.telegram.yml build
docker compose -f docker-compose.telegram.yml run --rm telegram-intelligence telegram login
docker compose -f docker-compose.telegram.yml up telegram-intelligence
```

上述 `up` 以前台方式运行时，先按 `Ctrl+C` 停止容器，然后执行：

```powershell
docker compose -f docker-compose.telegram.yml down
```

若以 `up -d` 启动，直接用同一条 `down` 停止。不要附加 `--volumes`/`-v`，除非操作员明确要求
删除 named volume 中的会话与专属 SQLite 数据，并已完成可恢复备份。

私有配置目录必须包含 `chats.yaml` 与 `keywords.yaml`。Docker Desktop 在 Windows 上挂载的仍
必须是 Linux `libtdjson.so`，不能使用 `tdjson.dll`。Compose 中 OpenAI 和媒体下载保持禁用，
告警默认禁用且 `DRY_RUN` 默认开启。宿主管理员可以从容器元数据看到环境变量，因此该方式只
适用于受信本机；它没有把凭据变成 Docker secret，也不应被描述为生产密钥管理方案。

容器未映射健康端口，可从容器内部查看：

```powershell
docker compose -f docker-compose.telegram.yml exec telegram-intelligence python -m backend.telegram_intelligence telegram status --timeout 2
```

## 日志、恢复与常见错误

当前交付不承诺专用日志文件。直接 CLI 将有界 JSON 写到 stdout；健康 HTTP handler 禁止访问
日志。容器日志由 Docker 管理，可用 `docker compose -f docker-compose.telegram.yml logs`
在受信终端查看。`LOG_LEVEL` 是运行设置，但不能据此假设消息内容或秘密已被安全归档；不要把
原始 Telegram 文本、登录码、2FA 密码、API hash、Bot token 或会话路径添加到日志。

恢复原则：

- 配置失败：修正未知字段、重复 ID、缩进或范围后重新启动；不要放宽严格解析。
- TDLib 加载失败：核对绝对路径、平台/位数、文件权限和官方动态依赖。
- 授权超时：重新运行 `telegram login`；先保留会话目录，不要用删除会话作为首个排障步骤。
- SQLite 恢复：先停止唯一写进程，再把 `.sqlite3`、`-wal`、`-shm` 作为一个单元备份/恢复；
  永远不要替换或复用 `collaboration_studio.sqlite3`。
- 重试积压：通过 `/status` 查看 `retry_jobs`、`dead_letter_jobs` 与 queue；`DEAD` 工作应先审查
  错误类别和幂等键，再决定是否重新入队，不能盲目无限重放。
- 存储水位：`storage_status=warning` 时安排停机备份与归档评审；`full` 时新准入会失败关闭。
  不要在线删除 WAL/SHM，也不要用清空库来掩盖容量问题；容量拒绝期间可能需要有界历史补采。
- Bot 失败：保持 `DRY_RUN=true` 核对固定目的地和格式；不要添加动态目的地、转发或群管理
  作为排障手段。

## 已知限制与风险

- **尚无真实集成验收。** 当前只有离线 Fake/单元/静态合同证据；未加载真实 TDLib、未登录真实
  Telegram、未执行 OpenAI 或 Bot 网络调用，也未构建/运行 Docker 镜像。OpenAI 仍被宿主
  策略硬禁用；任何 key 是否存在都不构成调用授权。
- **Bot 交付不是端到端 exactly-once。** 外部 Bot API 接受消息与本地持久化 `SENT` 之间存在
  崩溃窗口；重启后可能重试而造成重复，或处于无法自动判定的交付状态。因此运营上必须按
  at-least-once/不确定交付处理；已成功发送的告警不会因源消息后续编辑、删除或事件撤回而
  自动撤回。工作在入队时绑定当前数字目的地、来源版本、白名单状态和 15 分钟新鲜度截止；
  过期、改投或撤权的旧告警会被抑制，但这不能消除请求进行中的崩溃窗口。
- **AI 调用也不是端到端 exactly-once。** Provider 接受请求与本地原子写入缓存/用量之间仍有
  崩溃或超时不确定窗口，恢复后可能重复一次可计费推理。租约会在外部调用前后续约并做 fencing，
  但进程在请求进行中退出仍无法撤销上游已接收的调用。
- **采集边界仍存在异常退出恢复空档。** 正常 `SIGINT`/`SIGTERM` 会先冻结 TDLib 接收端并把有界
  内存队列送入原有持久化入口，再确认 worker 已退出后才报告 stopped；超过关停期限会显式报错，
  不会伪装成干净退出。但强杀、进程在更新落库前崩溃、队列溢出、TDLib 关闭失败或存储容量门
  拒绝写入仍可能留下空档。白名单内的有界历史补采可用于手动恢复，但不是无丢失保证。
- **运行聚类窗口有界，但没有自动保留/归档。** 事件聚类每次只加载时间窗口内最近事件，默认
  250 个且代码上限为 500。专属 SQLite 的配置上限默认 4 GiB；在 90% warning 水位就会停止
  新消息/任务准入和新的 backlog 领取，为已在途单个工作与关停记账保留 10%。这仍不是自动保留、
  压缩、归档或媒体/数据库清理任务，进入水位后必须由操作员处理，且停写期间需要评估历史补采。
- **内容保护依赖宿主。** 专属 `telegram.sqlite3`、`-wal`、`-shm`、消息修订与删除内容默认以
  明文形式持久且没有自动删除期。目录/文件权限只是一层约束；真实数据需要受限宿主 ACL、
  全盘加密、备份访问控制和明确的后续保留流程。
- **媒体下载尚未实现。** `DOWNLOAD_MEDIA=false` 是安全默认；即使将标志改为 `true`，当前管线
  仍只处理媒体元数据，不应被解读为会下载、扫描或分析文件。
- **AI 档位仍共用同一分析器/模型。** 本地预评分可选择 light/full 分析路由，但当前两个档位没有
  独立 Provider 或模型配置；不应宣称已实现分档模型成本/质量策略。
- **Source Inbox 只是手动投影。** 它不会自动写入 Studio 正式库、启动房间或提升任何交付状态；
  输出仍是 `external_unverified` 级别的信息来源。
- **永远没有交易权限。** 本阶段没有下单、仓位、钱包、签名、经纪商/交易所 API 或自动交易通道；
  `recommended_action` 仅是 `ignore/store/watch/alert/urgent_alert` 的信息路由动作，不是投资建议。

## 离线测试

Telegram 测试使用 Fake TDLib、Fake Analyzer、Fake Bot、临时 SQLite 与静态文件合同，不应发起
真实网络请求。PowerShell 中先隔离本机环境，再运行定向测试：

```powershell
python scripts\run_backend_tests_isolated.py --start-directory tests --pattern "test_telegram_*.py" --verbosity 2
```

仅验证本交付文件的静态合同：

```powershell
python scripts\run_backend_tests_isolated.py tests.test_telegram_delivery_contract --verbosity 2
```

静态合同只检查文档、示例、安全默认值、Docker 构建上下文/只读挂载及无端口映射；它不执行
Docker，也不证明 TDLib ABI、Telegram 登录、实时采集、Bot 投递或 Provider 网络调用成功。
