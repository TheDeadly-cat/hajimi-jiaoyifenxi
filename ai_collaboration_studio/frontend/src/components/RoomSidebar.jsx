import { ClipboardList, Plus, Search, X } from "lucide-react";
import { useMemo, useRef } from "react";
import { groupedRooms } from "../roomCategories";
import { useModalFocus } from "../useModalFocus";
import "../styles/room-sidebar-polish.css";

function roomTimeDetails(timestamp, today) {
  if (!timestamp) return null;
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return null;
  const sameDay = date.toDateString() === today.toDateString();
  return {
    dateTime: date.toISOString(),
    label: sameDay
      ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
      : date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" }),
    fullLabel: date.toLocaleString("zh-CN", {
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }),
  };
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
  const searchInputRef = useRef(null);
  const roomGroups = useMemo(() => groupedRooms(rooms, search), [rooms, search]);
  const visibleRoomCount = roomGroups.reduce((total, group) => total + group.rooms.length, 0);
  const totalRoomCount = rooms.length;
  const normalizedSearch = search.trim();
  const renderedAt = new Date();
  const resultSummary = normalizedSearch
    ? `${visibleRoomCount}/${totalRoomCount} 个匹配`
    : `${totalRoomCount} 个房间`;
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
      <div className="search-box" role="search" aria-label="房间搜索">
        <Search className="room-search-icon" size={16} aria-hidden="true" />
        <input
          ref={searchInputRef}
          type="search"
          aria-label="搜索房间"
          aria-controls="room-sidebar-list"
          aria-describedby="room-search-status"
          value={search}
          onChange={(event) => onSearch(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape" && search) {
              event.preventDefault();
              onSearch("");
            }
          }}
          placeholder="搜索房间"
        />
        {search && (
          <button
            className="room-search-clear"
            type="button"
            aria-label="清除房间搜索"
            title="清除搜索"
            onClick={() => {
              onSearch("");
              searchInputRef.current?.focus();
            }}
          >
            <X size={14} aria-hidden="true" />
          </button>
        )}
      </div>
      <div className="sidebar-section-label">
        <span>{normalizedSearch ? "搜索结果" : "房间大类"}</span>
        <small id="room-search-status" role="status" aria-live="polite" aria-atomic="true">
          {resultSummary}
        </small>
      </div>
      <nav
        id="room-sidebar-list"
        className="room-list"
        aria-label={normalizedSearch ? "房间搜索结果" : "房间列表"}
      >
        {roomGroups.map((group) => (
          <section className="room-category-group" aria-label={`${group.name}房间`} key={group.name}>
            <div className="room-category-heading"><span>{group.name}</span><small>{group.rooms.length} 个</small></div>
            {group.rooms.map((room) => {
              const active = room.id === activeRoomId;
              const timestamp = roomTimeDetails(room.last_message_at || room.updated_at, renderedAt);
              return (
                <button
                  key={room.id}
                  className={active ? "room-row active" : "room-row"}
                  type="button"
                  aria-current={active ? "page" : undefined}
                  onClick={(event) => onSelect(room.id, event.currentTarget)}
                >
                  <span className="room-dot" aria-hidden="true" />
                  <span className="room-copy">
                    <span className="room-title-line">
                      <strong title={room.title}>{room.title}</strong>
                      {active && <span className="room-current-label" aria-hidden="true">当前</span>}
                    </span>
                    <small className="room-meta">{room.subcategory_label} · {room.member_count || 0} 位成员</small>
                    {timestamp && (
                      <time
                        dateTime={timestamp.dateTime}
                        title={timestamp.fullLabel}
                        aria-label={`最后更新：${timestamp.fullLabel}`}
                      >
                        {timestamp.label}
                      </time>
                    )}
                  </span>
                </button>
              );
            })}
          </section>
        ))}
        {!visibleRoomCount && (
          <div className="empty-note">
            <span className="empty-note-icon" aria-hidden="true"><Search size={18} /></span>
            <strong>{normalizedSearch ? "没有匹配的房间" : "还没有房间"}</strong>
            <p>
              {normalizedSearch
                ? `没有找到包含“${normalizedSearch}”的房间，可清除关键词后重新浏览。`
                : "创建一个房间后，协作记录会按类别显示在这里。"}
            </p>
            {normalizedSearch && (
              <button
                className="secondary room-empty-reset"
                type="button"
                onClick={() => {
                  onSearch("");
                  searchInputRef.current?.focus();
                }}
              >
                清除搜索
              </button>
            )}
          </div>
        )}
      </nav>
    </aside>
  );
}
