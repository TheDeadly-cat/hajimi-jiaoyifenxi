import { Folder, MessageSquare, Sparkles, Users } from "lucide-react";

const items = [
  { icon: MessageSquare, label: "讨论房间", section: "rooms" },
  { icon: Users, label: "成员身份", section: "members" },
  { icon: Folder, label: "共享资料", section: "materials" },
  { icon: Sparkles, label: "共创产物", section: "artifacts" },
];

export function IconRail({ activeSection, onNavigate, onPreloadInspector }) {
  return (
    <nav className="icon-rail" aria-label="全局导航">
      <div className="brand-mark" aria-label="AI 共创室">AI</div>
      <div className="rail-actions">
        {items.map(({ icon: Icon, label, section }) => (
          <button
            key={label}
            className={activeSection === section ? "rail-button active" : "rail-button"}
            title={label}
            aria-label={label}
            onClick={(event) => onNavigate(section, event.currentTarget)}
            onFocus={section === "rooms" ? undefined : onPreloadInspector}
            onPointerDown={section === "rooms" ? undefined : onPreloadInspector}
            onPointerEnter={section === "rooms" ? undefined : onPreloadInspector}
          >
            <Icon size={20} strokeWidth={1.8} />
          </button>
        ))}
      </div>
    </nav>
  );
}
