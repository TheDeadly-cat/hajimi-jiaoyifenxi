# 五条公开 RSS 摘录的独立对照

仅用于明确批准的豆包 Seed 2.1 Pro 与 DeepSeek Flash 小样本对照。不是生产分析队列，不部署或迁移生产；正常产品的跨模型去重规则不变。

`prepare` 接受一个 JSON 数组，恰好五条不同的公开官方 RSS 摘录。字段仅 `id/source_id/source_name/url/title/excerpt/published_at/discovered_at/updated_at`；无实质摘录会拒绝。只复制冻结数据、当前 `news_content_quality.py`、`news_analysis_providers.py` 及其 `execution_boundary.py` 依赖，不调用来源或模型。每条两模型使用相同的冻结检查时间、输入和该候选的质量合同（当前为 v4；旧 v3 冻结运行保留），按豆包/Flash交替执行。旧结果只能作为历史参考，不充当本轮对照结果。

```powershell
& '<python.exe>' -I -S -B -X utf8 '<runner.py>' prepare --samples '<five-public-items.json>' --samples-sha256 '<SHA256>' --out '<new-run-directory>' --max-reserved-micro-cny 452154
& '<python.exe>' -I -S -B -X utf8 '<runner.py>' inspect --plan '<new-run-directory>\plan.json' --plan-sha256 '<SHA256>'
```

`plan.json` 绑定五条样本、三份冻结候选代码、执行器与遮罩输入代码、每次完整请求哈希、每次预约、总预约、固定两模型、900 输出 tokens、45 秒 socket timeout、零重试。通过 `--max-reserved-micro-cny` 绑定本次精确批准的正整数微元上限，最高不超过工具兼容上限 1,000,000 微元；实际总预约必须不超过该上限。示例452154微元即0.452154元，不代表另获1元授权。最多十个 `(sample_id,provider,model)` 唯一位置。DeepSeek 用峰值价及 10 元/美元预算参数；不假定缓存或低峰折扣。预约不是供应商账单硬上限；45 秒不是完整墙钟硬截止。

授权 JSON 使用生成的 `authorization.template.json` 结构，必须 `approved:true`、匹配 plan SHA 和 grant_id、固定 max_calls10，max_reserved_micro_cny必须与计划的本次精确上限一致、明确的直接用户批准引用，以及最长一小时的 `approved_at/expires_at`。确认用户已经批准本轮计划后记录此授权；模板本身不授权。主线程负责在执行前核实当前费率、样本来源及用户批准。

```powershell
& '<python.exe>' -I -S -B -X utf8 '<runner.py>' execute --plan '<plan.json>' --plan-sha256 '<SHA256>' --authorization '<authorization.json>' --authorization-sha256 '<SHA256>' --allow-real-provider
```

`execute` 排他写 `started.json` 后才显示两个本机遮罩输入框，支持 Ctrl+V、右键粘贴和字符数。未输入两项不发请求；关闭/取消、失效均不重放本计划。密钥只在本进程内存，不读环境或已有凭据，不入参数、文件、日志；Python 不能保证内存物理清零。进度窗口可取消尚未开始的请求，当前请求只等结束。不要向已结束终端粘贴。

每次请求前先在独立 `comparison.sqlite3` 内提交预约，再排他写 spent 回执；事务内同时核对本次精确预算上限和10次上限，GUI及起止回执显示同一上限。失败、未知、取消后不可退回或重试。401/403 停止该供应商余下位置，另一供应商已有独立授权的首次请求仍可进行；完整性/执行异常终止批次。一个计划不能再次启动，关闭 GUI 不能重置次数。异常后的 `reserved_unknown` 行必须保持已消耗/未知，不允许补跑。

模型适配器来自冻结候选，最后一层 opener 增加只读采集 HTTP、白名单 usage、模型身份和输出的包装，**显式直连 ProxyHandler({})**，不读取环境代理；这是独立验收传输选择，不声称验证生产代理路径。无重定向、探测、工具调用、外部来源抓取、回退、重试。仅保存本次公开材料产生的输出、受控错误类别、数字状态/usage；不保存 HTTP headers、原始错误正文或异常文本。结构及引文通过不代表语义真确，需要后续人工/独立复核。

必要离线验证：

```powershell
& '<python.exe>' -I -S -B -X utf8 '<tests\test_runner.py>'
& '<python.exe>' -I -S -B -X utf8 '<tests\test_credentials.py>'
```

测试只用合成材料、专用临时数据库和假 HTTP，禁止真实 DNS/socket。它覆盖固定成对输入、哈希、授权、永久预约和重复位置、预算、单次传输、HTTP 安全投影、两把密钥的直接及 JSON 转义回显拒绝、坏输出不重试和禁止生产数据库。另用 MockTk 验证双输入框遮罩粘贴、取消、过期和关闭后不能再次提交。不会启动真实输入窗口，不代表真实 API 或账单验收。
