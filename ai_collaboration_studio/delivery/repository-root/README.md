# AI 共创室（交易分析工作区）

本仓库是本地非 Git 权威源码目录的 source-only 发布投影。主产品位于 `ai_collaboration_studio/`；仓库根文件只负责说明、受控启动和 CI 发现，不是第二份产品源码。

本地工作区可以另行保留 `TradingAgents/` 作为外部研究参考，但公开投影不包含、合并或重新发布该第三方项目。

启动 AI 共创室：双击 `run_ai_collaboration_studio.cmd`。根脚本只转交给 `ai_collaboration_studio/scripts/start_ai_collaboration_studio.ps1`；由后者核验 readiness、host version、当前后端源码指纹和集成清单，再决定打开现有实例或启动本地服务。旧实例、被占用端口、待迁移数据库或缺失 production frontend 都会失败关闭，不会被静默停止、替换或迁移。

GitHub Actions 的实际入口必须位于仓库根 `.github/workflows/isolated-validation.yml`。该文件由项目内同名 canonical 模板确定性投影，所有运行步骤以 `ai_collaboration_studio/` 为工作目录。

安全边界：系统只进行研究、回测和模拟决策，不提供真实下单、支付、钱包或资金动作。Provider、Futu/OpenD、SEC 与 IR 均受各自配置、权限和失败关闭边界约束；代码、CI 或启动成功不代表数据真实、模型可用或交易获准。
