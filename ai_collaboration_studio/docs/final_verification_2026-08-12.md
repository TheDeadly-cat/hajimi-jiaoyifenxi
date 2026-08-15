# 最终隔离复核（2026-08-12）

本文件记录当前工作树的最新验证结果；不代表正式数据库迁移授权。

## 迁移硬门

- `backend/path_identity.py` 在 owner 锁、迁移 source、verified-startup、shadow 和 Store 入口解析前逐级检查原始路径链，拒绝父目录 symlink、junction/reparse alias。
- manifest、backup、candidate、prepared、receipt 以及底层 copy/lease/publish/replace 原语也在 `resolve()` 前检查原始路径链，拒绝输出目录别名。
- intent marker 现在拒绝大小写变体、symlink/reparse 和 hardlink 文件，并保持 invalid marker 为 active blocking 状态。
- marker + migration hard-edge 定向回归：`29/29 OK`。
- migration 层：`87/87 OK`。
- 启动仍是只读预检；正式 `apply` 未执行。

## 完整回归

- 后端完整隔离回归：`1222/1222 OK`，`1149.455s`。
- runtime：`C:\Users\Administrator\AppData\Local\Temp\ai-collaboration-studio-tests-cn0yf2g5`。
- 前端：`317/317`；Vite `1673 modules`，production build 成功且无大块 chunk 警告。

## 最新源码归档

- 归档目录：`C:\Users\Administrator\AppData\Local\Temp\ai-collaboration-studio-source-backups-final5-8bb03ec09b7d45c397292778afb90d39`
- 522 文件，源总字节 `25,408,009`，manifest 总 SHA-256 `5c2a4fa1cef0063c946b6f48c29db8c10c4a6af1e0afe79f0a5989aaa4852b29`
- ZIP 大小 `17,771,504`，archive SHA-256 `3ba7a6f7e06040a2e4b5a5d9c0428d4355de0ffe6d6812e484ae1cabdfad9ef0`

## 正式环境边界

- 正式 SQLite 主文件未写入：SHA-256 `B32E88A0C0BE5DB2D052904221C6C85D1B1C7862FD76F45EB8DF08B7EC41CC05`，大小 `5,062,656`。
- 只读文件核验看到 `-wal` 0 字节、`-shm` 32 KiB、无 journal；未清理或修改这些 sidecar。
- 8770 与 11111 无监听；未调用 Provider、Futu/OpenD 或外部网络；项目仍未初始化 Git。

## 仍需用户明确授权

1. 正式库永久备份位置与迁移窗口；
2. 审阅 manifest/prepared/backup 的精确哈希后提供授权 token，才可执行 `apply`；
3. 任何真实 Provider、Futu/OpenD、SEC/IR 或市场数据连接。

P28 空迁移账本、真实概率校准包和自动执行能力仍按边界不实现。
