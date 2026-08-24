import { Folder, MessageSquare, Sparkles, Users } from "lucide-react";
import "../styles/icon-rail-polish.css";

const items = [
  { icon: MessageSquare, label: "讨论房间", section: "rooms" },
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

export function IconRail({ activeSection, onNavigate, onPreloadInspector }) {
  return (
    <nav className="icon-rail" aria-label="全局导航">
      <div className="brand-mark" role="img" aria-label="AI 共创室">AI</div>
      <div className="rail-actions">
        {items.map(({ icon: Icon, label, section }) => (
          <button
            key={section}
            className={activeSection === section ? "rail-button active" : "rail-button"}
            type="button"
            title={label}
            aria-label={label}
            aria-current={activeSection === section ? "page" : undefined}
            tabIndex={activeSection === section ? 0 : -1}
            data-label={label}
            data-section={section}
            onClick={(event) => onNavigate(section, event.currentTarget)}
            onKeyDown={moveRailFocus}
            onFocus={section === "rooms" ? undefined : onPreloadInspector}
            onPointerDown={section === "rooms" ? undefined : onPreloadInspector}
            onPointerEnter={section === "rooms" ? undefined : onPreloadInspector}
          >
            <Icon size={20} strokeWidth={1.8} aria-hidden="true" focusable="false" />
          </button>
        ))}
      </div>
    </nav>
  );
}
