# AI 共创室（交易分析工作区）

双击 **打开消息工作台.cmd**。首次打开会显示中文设置向导：检查环境、说明需要安装的组件、等待确认，再创建独立消息数据。以后仍双击这个入口回到自己的消息列表，不需要输入命令、选择分支或手工修改配置。

不配置 AI 也能阅读和收集美联储、美国劳工统计局（BLS）及希捷官方订阅。AI 是可跳过的功能，自动分析默认关闭；配置保存不等于密钥已经验证，首次真实解释仍需明确操作。阅读方法见[使用说明](ai_collaboration_studio/使用说明.txt)。

当前分支是基于 PR #38 的独立新手体验候选，尚不代表已替换本机运行版本。统一入口复用现有锁定依赖安装和独立数据初始化，不自动覆盖、迁移或恢复已有数据，也不会停止占用端口的程序。旧 `run_ai_collaboration_studio.cmd` 仅作为同一向导的兼容入口。

维护人员的命令、依赖锁和隔离核验见[高级安装与启动说明](ai_collaboration_studio/docs/NEWS_PORTABLE_DELIVERY.md)；普通首次使用不需要执行其中的命令。

验证结论见[交付边界](ai_collaboration_studio/docs/NEWS_DELIVERY_ACCEPTANCE.md)和[分来源状态](ai_collaboration_studio/docs/NEWS_SOURCE_STATUS_20261024.md)。构建、服务启动、来源恢复与真实 API 审核质量分别验收。当前本机生产安装不由此入口切换。

## 原仓库说明（历史）

以下保留原发布投影说明；其中旧启动脚本和默认 8770 地址不适用于本分支的新检出。

本仓库是本地非 Git 权威源码目录的 source-only 发布投影。主产品位于 `ai_collaboration_studio/`；仓库根文件只负责说明、受控启动和 CI 发现，不是第二份产品源码。

本地工作区可以另行保留 `TradingAgents/` 作为外部研究参考，但公开投影不包含、合并或重新发布该第三方项目。

启动 AI 共创室：双击 `run_ai_collaboration_studio.cmd`。根脚本只转交给 `ai_collaboration_studio/scripts/start_ai_collaboration_studio.ps1`；由后者核验 readiness、host version、当前后端源码指纹和集成清单，再决定打开现有实例或启动本地服务。旧实例、被占用端口、待迁移数据库或缺失 production frontend 都会失败关闭，不会被静默停止、替换或迁移。

GitHub Actions 的实际入口必须位于仓库根 `.github/workflows/isolated-validation.yml`。该文件由项目内同名 canonical 模板确定性投影，所有运行步骤以 `ai_collaboration_studio/` 为工作目录。

安全边界：系统只进行研究、回测和模拟决策，不提供真实下单、支付、钱包或资金动作。Provider、Futu/OpenD、SEC 与 IR 均受各自配置、权限和失败关闭边界约束；代码、CI 或启动成功不代表数据真实、模型可用或交易获准。
