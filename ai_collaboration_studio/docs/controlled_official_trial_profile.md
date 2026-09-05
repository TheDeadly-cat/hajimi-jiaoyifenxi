# PR B：受限官方来源配置

`AI_STUDIO_SOURCE_MONITOR_PROFILE=sec_micron_trial_v1` 选择代码内唯一受限试用配置。
空值沿用原默认目录；未知名称、URL、股票列表不被接受。选择 profile 本身不启用监控，
也不改变默认的 disabled、auto-start off、dry-run 状态。

| 来源 | 固定范围 | 单轮内容额度 | 轮询周期 |
| --- | --- | ---: | ---: |
| SEC | US.NVDA / 8-K / submissions recent；累计身份容量仍为 1,000 | 3 | 5 分钟 |
| 公司 IR | US.MU / Micron 官方 recent-30 JSON 与公告页头时间 | 8 | 5 分钟 |

初始化只能为 `seed_only`，持续 cutoff 关闭，只允许官方 pipeline；行情、其他初始化模式
或低于 8 的全局单轮额度与该 profile 冲突时，在来源请求前明确报错，不静默改用默认范围。
采集、去重、已阅、挂接、研究草稿不调用模型；后续深度研究仍是单独的人工作业与预算决定。

## 同一份范围定义

`backend/source_monitoring/profiles.py` 提供闭合 manifest，生产 Registry 直接据此构造两个
适配器。Runtime、独立 Supervisor、操作员服务、CLI 和既有 soak CLI 都复核其注册范围。
默认路径保留原行为，试用路径不构造宏观或 Futu adapter。

操作员 control v3 返回同一 manifest（默认 control 仍 v2）；试用 health v4 返回同一
profile 的设置和目录（默认 health 仍 v3）。HTTP health 读取已构造 Runtime 的 settings，
避免再次读取环境造成“显示范围”与实际 worker 不一致。数据库中其它来源的旧状态可作为
未注册、非 effective 的历史信息保留，不能因此变成试用监控源。

范围摘要：`e6f07d2c97d71c1280ab653352fc019cd99845be5bd06468ae6ace65899454e3`。
这是配置身份，不是网络可达、来源真实性、真实新增或长期健康证明。

## 预检和启用

试用预检复用现有操作员 initialization-preview 或
`python -B -m backend.source_monitoring_cli preview <adapter_key>`，它们使用与 Runtime 相同
的 profile Registry。应按现有 owner、状态版本、配置版本和本地确认流程执行；受限试用
使用独立临时数据库，不对正式数据库执行这些操作。

旧 `scripts/run_sec_ir_live_preflight.py` 的范围固定为原 SEC/default IR RSS，不能证明该
试用配置。选中命名 profile 时，其 CLI 与后端入口都会在网络请求前返回
`PREFLIGHT_PROFILE_REQUIRES_OPERATOR_PREVIEW`，避免小范围配置下误运行大范围预检。

同一进程启动前选择范围的配置示例（不会授权数据库迁移或正式部署）：

```powershell
$env:AI_STUDIO_SOURCE_MONITOR_PROFILE = 'sec_micron_trial_v1'
$env:AI_STUDIO_SOURCE_MONITOR_OFFICIAL_ONLY = '1'
$env:AI_STUDIO_SOURCE_MONITOR_ALLOW_READONLY_MARKET = '0'
$env:AI_STUDIO_SOURCE_MONITOR_INITIAL_MODE = 'seed_only'
```

启用、auto-start、dry-run 以及每个 adapter 的本地确认仍使用既有门。SEC 联系型 User-Agent
只从执行进程环境读取，不写入此文档、前端、profile 或源码。

## 验证范围

真实生产构造、CLI preview→Runtime 完整 seed、健康与控制目录一致性、错误 scope/设置
拒绝，以及 legacy RSS probe 拒绝命名 profile 的组合测试均使用临时数据库和离线 transport。
13 条 SEC / 30 条 IR 初始化保持 0 个新增 Inbox 项，Provider 账本和正式轮次为 0。
HTTP 测试另验证：Runtime 构造后环境发生变化，健康与控制面仍显示同一实际 Runtime 范围。

这些证明配置和调用链一致，不代表默认其它来源在线通过。浏览器和实际网络观察分别记录，
不能用 fixture 通知充当真实新公告。PR C 的 reader 发布门和 PR D 的元数据缓存是后续独立提交。
