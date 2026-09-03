import { Folder, Inbox, MessageSquare, PawPrint, Sparkles, Users } from "lucide-react";
import "../styles/icon-rail-polish.css";

const items = [
  { icon: MessageSquare, label: "讨论房间", section: "rooms" },
  { icon: Inbox, label: "来源收件箱", section: "source-inbox" },
  { icon: Users, label: "成员身份", section: "members" },
  { icon: Folder, label: "共享资料", section: "materials" },
  { icon: Sparkles, label: "共创产物", section: "artifacts" },
];

const railFocusKeys = new Set([
  "ArrowDown",
  "ArrowUp",
  "ArrowRight",
  "ArrowLeft",
  "Home",
  "End",
]);

const MAX_SOURCE_INBOX_UNREAD_COUNT = 100;

function sourceInboxUnreadBadge(value) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
    return { count: 0, label: "", text: "" };
  }
  const count = Math.min(value, MAX_SOURCE_INBOX_UNREAD_COUNT);
  return count === MAX_SOURCE_INBOX_UNREAD_COUNT
    ? { count, label: "99 条以上未读", text: "99+" }
    : { count, label: `${count} 条未读`, text: String(count) };
}

function moveRailFocus(event) {
  if (!railFocusKeys.has(event.key)) return;
  const buttons = Array.from(
    event.currentTarget
      .closest(".rail-actions")
      ?.querySelectorAll(".rail-button:not(:disabled)") || [],
  );
  if (!buttons.length) return;
  const currentIndex = buttons.indexOf(event.currentTarget);
  let nextIndex = currentIndex;
  if (event.key === "Home") nextIndex = 0;
  if (event.key === "End") nextIndex = buttons.length - 1;
  if (event.key === "ArrowDown" || event.key === "ArrowRight") {
    nextIndex = (currentIndex + 1) % buttons.length;
  }
  if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
    nextIndex = (currentIndex - 1 + buttons.length) % buttons.length;
  }
  event.preventDefault();
  buttons[nextIndex]?.focus();
}

export function IconRail({
  activeSection,
  onNavigate,
  onPreloadInspector,
  onPreloadSourceInbox,
  sourceInboxUnreadCount = 0,
}) {
  const unreadBadge = sourceInboxUnreadBadge(sourceInboxUnreadCount);
  return (
    <nav className="icon-rail" aria-label="全局导航">
      <div className="brand-mark" role="img" aria-label="AI 共创室·值班喵">
        <PawPrint size={22} strokeWidth={2.1} aria-hidden="true" />
      </div>
      <div className="rail-actions">
        {items.map(({ icon: Icon, label, section }) => {
          const preload = section === "source-inbox"
            ? onPreloadSourceInbox
            : section === "rooms"
              ? undefined
              : onPreloadInspector;
          const accessibleLabel = section === "source-inbox" && unreadBadge.count
            ? `${label}，${unreadBadge.label}`
            : label;
          return (
            <button
              key={section}
              className={activeSection === section ? "rail-button active" : "rail-button"}
              type="button"
              title={accessibleLabel}
              aria-label={accessibleLabel}
              aria-current={activeSection === section ? "page" : undefined}
              tabIndex={activeSection === section ? 0 : -1}
              data-label={accessibleLabel}
              data-section={section}
              onClick={(event) => onNavigate(section, event.currentTarget)}
              onKeyDown={moveRailFocus}
              onFocus={preload}
              onPointerDown={preload}
              onPointerEnter={preload}
            >
              <Icon size={20} strokeWidth={1.8} aria-hidden="true" focusable="false" />
              {section === "source-inbox" && unreadBadge.count ? (
                <span className="source-inbox-unread-badge" aria-hidden="true">
                  {unreadBadge.text}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
    </nav>
  );
}
