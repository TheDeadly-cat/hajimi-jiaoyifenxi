# 受控联网采集：沿用人工试用入口，显式选择联网模式

离线入口保持默认 `-Mode Offline`：监控与自动启动关闭，dry-run 开启，所有 Provider 禁用。
它不因外层环境变量或已有 Adapter 启用状态而开始采集。离线模式收到联网配置会明确拒绝。

联网仍使用同一个前台 `server.py`、数据库 owner、迁移门、Runtime、Scheduler 和收件箱。
只有显式 `-Mode Online -NetworkConfigPath <本机配置>` 才开启全局监控与自动启动，并关闭 dry-run。
固定范围是 `sec_micron_trial_v1`：NVDA 8-K、Micron recent-30、300 秒、seed_only；
不启用影响规则或任何 Provider，不执行自动 AI 分析、正式讨论或交易。

## 一次性准备

1. 使用单独确认的 LOCALAPPDATA 持久数据目录，按现有备份／初始化流程准备独立数据库。
   不复制正在写入的 SQLite 主文件，不删除 WAL，不修改正式数据或解除初始化门。
2. 版本配置沿用 `studio_supervised_trial_v1`：绑定候选 SHA、后端指纹、完整前端清单、
   独立数据目录、数据库和空闲的回环端口。来源开关和 checkpoint 保存在这个数据库中。
3. 将应用／组织名和联系邮箱写入源码目录之外的本机 UTF-8 单行文件；不要提交文件或打印内容。
4. 创建本机联网配置（以下路径仅说明格式，应替换为本机实际批准目录）：

```json
{
  "format_version": "studio_source_collection_v1",
  "data_directory": "C:\\Users\\YOUR_USER\\AppData\\Local\\AI Collaboration Studio\\OnlineCollection",
  "sec_user_agent_file": "C:\\Users\\YOUR_USER\\Documents\\AI共创室试用\\联网采集\\SEC联系标识.txt",
  "sec_route": "inherit"
}
```

`data_directory` 必须与版本配置一致。`inherit` 沿用当前网络；`sec_direct_only` 仅在
当前启动进程的 NO_PROXY 追加 www.sec.gov 与 data.sec.gov，Micron 仍走现有网络。
选择路由须符合实际授权，不改全局代理、URL、TLS 或生产超时。退出时恢复父 PowerShell 的环境。

## 启动和逐来源启用

在项目目录运行：

```powershell
.\scripts\start_supervised_trial.ps1 -ConfigPath '<本机版本配置.json>' -Mode Online -NetworkConfigPath '<本机联网配置.json>' -CheckOnly
.\scripts\start_supervised_trial.ps1 -ConfigPath '<本机版本配置.json>' -Mode Online -NetworkConfigPath '<本机联网配置.json>'
```

`-CheckOnly` 只核对文件、版本、模式和端口；不打开数据库或访问来源，不是联网成功证据。
保留控制台，看到 `server_started` 后打开输出的地址。网页「来源收件箱」内展开「来源运行状态」，
点击「查看 Adapter 接入设置」。首次点击来源卡片上的「预览首次读取范围」，
阅读范围与基线说明，勾选确认并保存启用。预览会访问该来源，需要在批准的联网窗口内执行。
保存首次启用时会重新读取来源核对预览，次数预算应包含这次读取。已有基线的停用来源显示「准备启用」。

此后由后台 worker 自动执行到期来源，无需反复点击 run-once。已启用状态重启后保留；
如果配置版本变化，按现有提示重新复核，不能清空 checkpoint 或强制改状态绕过。
单独停用某来源使用同一区域的「准备停用」和确认；不删除游标、消息或材料。

## 看状态和使用消息

- 查看当前进程生效配置中的全局开关、自动启动、dry-run／导入模式和固定范围。
- 查看每个来源的生效开关、基线、上次检查、最近成功、下次检查、本轮新增和错误码。
- 未启用不等于无新增；dry-run 只预览，不正式入库；基线未完成不应冒充正常连续检查。
- Micron 待重验／失败保持真实降级；已启用且健康的 SEC 可继续独立检查。
- seed_only 首次完成全部历史基线，不补发历史通知；后续真实新身份由收件箱去重入库。
- 点击通知或事件只展示详情。已阅、手选房间、附加、唯一研究草稿仍分别确认；
  「ChatGPT 协作」仍为人工任务包流程，采集和草稿不调用模型。

## 停止、恢复与验收边界

在所属控制台按 Ctrl+C，等 `server_stopped` 和命令提示符返回，再关闭窗口。
正常宿主在 worker、HTTP 请求收尾后、释放 owner 前由 SQLite 完成 WAL checkpoint；
成功输出 `server_database_checkpoint_completed`。存在活动 reader 或收尾失败时明确报错，
保留数据文件，不删除 sidecar 或放宽下次启动的只读迁移门。
关闭浏览器不会停止后台；异常收尾需核实该实例的端口、worker 和 owner，不能批量结束 Python。
重新执行相同入口使用同一数据库，沿用原基线、checkpoint、启用状态及研究资料。

真实来源验证须事先确定来源、数据目录、时长及停止条件；到期停止，不自动无限延长。
离线 fixture 只证明代码链路。真实可达、自动连续检查、重启恢复与自然新增全流程分别报告。
未出现自然新公告时保留“尚未观察”，不修改旧公告时间、ID 或游标制造成功。
本交付仅为自动采集；公告正文与有依据的板块影响分析是后续独立任务。
