import { ClipboardList, Plus, Search, X } from "lucide-react";
import { useMemo, useRef } from "react";
import { groupedRooms } from "../roomCategories";
import { useModalFocus } from "../useModalFocus";

function roomTime(timestamp) {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export function RoomSidebar({
  rooms,
  activeRoomId,
  search,
  mobileOpen = false,
  mobileModal = false,
  restoreFocusRef,
  onSearch,
  onSelect,
  onCreate,
  onOpenActions,
  onClose,
}) {
  const sidebarRef = useRef(null);
  const closeButtonRef = useRef(null);
  const roomGroups = useMemo(() => groupedRooms(rooms, search), [rooms, search]);
  const visibleRoomCount = roomGroups.reduce((total, group) => total + group.rooms.length, 0);
  useModalFocus({
    open: mobileModal && mobileOpen,
    containerRef: sidebarRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose,
  });
  return (
    <aside
      ref={sidebarRef}
      className={mobileOpen ? "room-sidebar mobile-open" : "room-sidebar"}
      role={mobileModal && mobileOpen ? "dialog" : undefined}
      aria-modal={mobileModal && mobileOpen ? "true" : undefined}
      aria-labelledby={mobileModal && mobileOpen ? "room-sidebar-title" : undefined}
      aria-hidden={mobileModal && !mobileOpen ? "true" : undefined}
      inert={mobileModal && !mobileOpen ? "" : undefined}
      tabIndex={mobileModal && mobileOpen ? -1 : undefined}
    >
      <div className="sidebar-brand">
        <span id="room-sidebar-title"><small>协作空间</small>AI 共创室</span>
        <button ref={closeButtonRef} className="icon-button sidebar-mobile-close" type="button" aria-label="关闭房间列表" onClick={onClose}><X size={18} /></button>
      </div>
      <button className="primary wide" type="button" onClick={(event) => onCreate(event.currentTarget)}><Plus size={17} />新建房间</button>
      <button className="secondary wide action-overview-entry" type="button" onClick={onOpenActions}>
        <ClipboardList size={16} />查看全部行动
      </button>
      <label className="search-box">
        <Search size={16} />
        <input aria-label="搜索房间" value={search} onChange={(event) => onSearch(event.target.value)} placeholder="搜索房间" />
      </label>
      <div className="sidebar-section-label"><span>房间大类</span><small>{visibleRoomCount} 个可见</small></div>
      <div className="room-list">
        {roomGroups.map((group) => (
          <section className="room-category-group" aria-label={`${group.name}房间`} key={group.name}>
            <div className="room-category-heading"><span>{group.name}</span><small>{group.rooms.length} 个</small></div>
            {group.rooms.map((room) => (
              <button
                key={room.id}
                className={room.id === activeRoomId ? "room-row active" : "room-row"}
                type="button"
                aria-current={room.id === activeRoomId ? "page" : undefined}
                onClick={(event) => onSelect(room.id, event.currentTarget)}
              >
                <span className="room-dot" />
                <span className="room-copy">
                  <strong>{room.title}</strong>
                  <small>{room.subcategory_label} · {room.member_count || 0} 位成员</small>
                </span>
                <time>{roomTime(room.last_message_at || room.updated_at)}</time>
              </button>
            ))}
          </section>
        ))}
        {!visibleRoomCount && <div className="empty-note">没有匹配的房间</div>}
      </div>
    </aside>
  );
}
