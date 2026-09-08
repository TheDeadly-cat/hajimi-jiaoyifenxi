import { Bell, BellOff, MonitorX, ShieldQuestion } from "lucide-react";

export function SourceInboxNotifications({ notificationState, onNotificationPreferenceChange }) {
  const supported = notificationState?.supported === true;
  const enabled = notificationState?.enabled === true;
  const permission = String(notificationState?.permission || "unsupported");
  const denied = permission === "denied";
  const active = supported && enabled && permission === "granted";
  const tone = !supported ? "unsupported" : denied ? "denied" : active ? "on" : "off";
  const Icon = !supported ? MonitorX : denied ? ShieldQuestion : enabled ? Bell : BellOff;
  const status = !supported ? "不支持" : denied ? "浏览器已阻止" : active ? "已开启" : "未启用";
  const hint = !supported
    ? "仍可在来源收件箱查看全部未读消息。"
    : denied
      ? "请在浏览器的本站点权限中允许通知，然后刷新页面。"
      : active
        ? "只提醒之后的新未读消息。请保留页面打开，可以最小化窗口。"
        : enabled
          ? "尚未取得浏览器权限。请先关闭提醒，再重新启用。"
          : "点击启用后，在浏览器的权限提示中选择「允许」。";

  return (
    <section className={`source-inbox-notify source-inbox-notify-${tone}`} aria-label="桌面提醒设置">
      <div className="source-inbox-notify-main">
        <span className="source-inbox-notify-icon" aria-hidden="true"><Icon size={18} /></span>
        <div className="source-inbox-notify-copy">
          <strong>桌面提醒 <em className="source-inbox-notify-status">{status}</em></strong>
          <small>{hint}</small>
        </div>
      </div>
      <div className="source-inbox-notify-action">
        <button
          className={enabled ? "secondary compact" : "primary compact"}
          type="button"
          disabled={!supported || denied}
          onClick={() => onNotificationPreferenceChange?.(!enabled)}
        >
          {!supported ? "浏览器不支持" : denied ? "已被浏览器阻止" : enabled ? "关闭桌面提醒" : "启用桌面提醒"}
        </button>
      </div>
      <details className="source-inbox-notify-help">
        <summary>提醒设置说明</summary>
        <p>浏览器权限和应用开关都启用后，才会提醒新未读消息。历史未读不会补发，启用时也不会立即产生测试提醒。关闭页面后不再推送。</p>
        <p>如果浏览器已阻止提醒，请打开地址栏左侧的站点权限，将通知设为「允许」，然后刷新本页。Windows 是否显示提醒还取决于系统通知设置；应用无法确认系统弹窗是否出现。</p>
      </details>
    </section>
  );
}
