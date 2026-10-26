// Presentation-only guidance derived from the existing public status fields.
const sourceNames = { fed: "美联储", bls: "美国劳工统计局（BLS）", seagate: "希捷公司公告" };

export function sourceDisplayName(source) {
  return sourceNames[source?.id] || source?.name || "名称待确认的来源";
}

export function sourceGuidance(source, { collectionRunning = false, workerStopped = false, workerUnresponsive = false } = {}) {
  if (source.enabled === false) return { title: "此来源已停用", impact: "暂不收集这个来源的新消息，已保存消息仍可阅读。", next: "可先阅读已有消息；如需更改订阅范围，请查看使用帮助。" };
  if (source.manual_hold === true) return { title: "等待人工核对", impact: "这个来源的新消息可能缺失，已有消息不受影响。", next: "允许访问的时间尚未确认。请保留现状，查看下方详情并交由维护人员核对；重复检查不会解除等待。" };
  if (source.cooldown_active === true) return {
    title: source.cooldown_reason === "rate_limited" ? "来源要求稍后访问" : "等待下次允许检查",
    impact: source.cooldown_reason === "access_denied" ? "暂时无法获取该来源，新消息可能缺失；已保存消息仍可阅读。访问被拒绝，原因待确认。" : "等待期间这个来源可能没有新消息，已有消息仍可阅读。",
    next: `${collectionRunning ? "无需重复点击。到下方允许时间后，按原计划再检查。" : "请先看上方后台状态；恢复收集后也会遵守下方等待时间。"}${source.cooldown_reason === "access_denied" ? "如果反复受限，请保留状态交由维护人员核对接入要求和请求路径。" : ""}`,
  };
  if (source.status === "fetching" && (workerStopped || workerUnresponsive)) return { title: "本次检查结果未确认", impact: "目前不能确认这个来源正在更新。", next: "可先阅读已保存消息；列表旁的重新读取按钮会更新消息和后台状态。" };
  if (source.status === "error" || source.status === "interrupted_unknown") return { title: source.cooldown_reason === "access_denied" ? "暂时无法获取该来源" : "最近检查未完成", impact: "这个来源的新消息可能缺失，已有消息仍可阅读。", next: "先查看下次检查时间。若需要帮助，可展开技术详情；不会因重复点击提前请求。" };
  if (source.recovery_pending === true) return { title: "正在恢复检查", impact: "已有成功记录，但还不能确认持续恢复。", next: "无需反复检查，系统仍按原恢复规则等待并逐步调整频率。" };
  if (source.stale || source.overdue) return { title: "检查记录已过期", impact: "旧消息仍可阅读，但不能据此确认这个来源仍在更新。", next: "查看上方后台状态和下次检查时间。" };
  if (source.status === "fetching") return { title: "正在检查这个来源", impact: "这次检查尚未完成。", next: "稍候查看结果，不需要重复操作。" };
  if (source.status === "ok") return { title: "最近一次检查成功", impact: "可以阅读已收集的消息；成功检查也可能没有新内容。", next: collectionRunning ? "继续按原计划收集，无需操作。" : "阅读已有消息不受影响；是否继续收集请看上方后台状态。" };
  return { title: source.status === "never" ? "尚未完成首次检查" : "来源状态待确认", impact: "目前还没有足够记录确认该来源的更新情况。", next: "可先查看已有消息与后台状态，不会因此自动启用AI。" };
}

export function runtimeHelp(startup) {
  if (startup?.mode === "portable") return {
    title: "独立运行版",
    description: "请从启动本次工作台的同一入口再次打开，继续使用这份独立数据。",
    login: "此运行方式不设置Windows登录后恢复，已有登录入口不会被改动。",
  };
  if (startup?.mode === "installed") return {
    title: "本机安装版",
    description: "下次从当前安装的原入口打开，继续查看已保存的消息。",
    login: startup.supported === true ? "如需登录Windows后恢复收集，可在下方“运行记录”设置；这不会自动配置AI。" : "当前登录恢复能力待确认，请先阅读已有消息，不要更换数据目录。",
  };
  return {
    title: "运行方式尚未确认",
    description: "请使用刚才打开本工作台的同一入口；当前不能据此判断是否为安装版或独立运行版。",
    login: startup?.supported === true ? "此服务报告支持登录恢复；具体设置与确认结果见下方“运行记录”。" : "当前不能确认登录恢复可用，请勿按其他版本的说明设置；免费阅读不受影响。",
  };
}
