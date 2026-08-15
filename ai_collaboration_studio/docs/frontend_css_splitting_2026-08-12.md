# 前端领域样式懒加载验收（2026-08-12）

## 结果

足球与股票只读研究面板的专属样式已从宿主首屏样式中拆出，并跟随各自的 React 懒加载组件按需加载。宿主仍保留房间设置所需的股票池输入样式和贡献状态样式，因此尚未打开研究面板时不会出现未样式化的设置控件。

本次调整不改变足球/股票合同、材料核验、正式轮授权、用户决定链或任何 Provider/市场连接边界。

## 源码边界

- `frontend/src/components/FootballResearchPanel.jsx` 显式加载 `frontend/src/styles/football-research.css`。
- `frontend/src/components/StockResearchPanel.jsx` 显式加载 `frontend/src/styles/stock-research.css`。
- `frontend/src/styles.css` 不再包含 `.football-*` 与面板专属 `.stock-research-*` / `.stock-*` 规则。
- `frontend/src/styles.css` 继续保留 `.stock-room-scope-field`、`.stock-room-scope-error` 与 `.stock-research-contribution-status`，因为这些规则属于宿主设置与贡献状态，而非懒加载面板。
- `frontend/tests/codeSplitting.test.js` 固定上述归属，防止后续把领域样式意外重新并回首屏 CSS。
- 移动端检查器标题由宿主 `App` 持有，并在 DOM 中排在足球/股票懒加载面板之前；关闭动作与视觉阅读顺序保持一致，领域面板仍由静态宿主贡献决定是否出现。

## 构建变化

| 产物 | 调整前 | 调整后 |
| --- | ---: | ---: |
| 宿主首屏 CSS | 242.81 kB / gzip 42.58 kB | 228.31 kB / gzip 40.01 kB |
| 足球面板 CSS | 并入宿主 | 8.69 kB / gzip 2.15 kB |
| 股票面板 CSS | 并入宿主 | 5.81 kB / gzip 1.61 kB |
| 主入口 JS | 482.39 kB / gzip 146.03 kB | 482.70 kB / gzip 146.09 kB |

宿主首屏 CSS 原始体积减少 14.50 kB（约 5.97%），gzip 体积减少 2.57 kB（约 6.04%）。最终 Vite 构建转换 1675 个模块，无大于 500 kB 的 chunk 警告。

## 验证

- 前端全量单元测试：320/320 通过。
- 生产构建：成功；足球和股票 CSS 均形成独立产物。
- 桌面 1280×720：新建临时足球/股票能力房间，展开两套只读面板；渐变、边框、表单与宿主检查器布局正常，无横向溢出。
- 移动端 390×844：房间信息标题位于抽屉 y=0，股票只读面板紧随标题，房间检查器正文随后；document/body 的 clientWidth 与 scrollWidth 均为 390，无横向溢出。
- 浏览器验收使用系统临时 runtime、显式临时 SQLite 与隔离端口 18772；验收后进程已停止，端口监听为 0。
- 未启动正式 8770，未连接 Futu/OpenD 11111，未调用 Provider，未读取或写入正式数据库。

## 后续建议

继续以真实使用频率为依据拆分其余体积较大的领域样式；不要为了追求 chunk 数量拆分宿主骨架、焦点态、错误态或跨页面共享的安全提示。每次拆分都应同时保留结构测试、生产构建产物检查和桌面/移动端渲染验收。
