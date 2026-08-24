import {
  AlertTriangle,
  ArrowDown,
  ArrowUpRight,
  Reply,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  CHAT_TIMELINE_CONTENT_LIMIT,
  CHAT_TIMELINE_MEMBER_LIMIT,
  CHAT_TIMELINE_TRANSIENT_ERROR_LIMIT,
  buildTimelineCitationPresentation,
  buildTimelineMemberIndex,
  buildChatTimelinePresentation,
  chatInitials,
  formatTimelineTime,
  normalizeTransientErrors,
  safeAvatarColor,
  safeTimelineText,
} from "../chatTimelineView";
import { nextChatAnnouncementState } from "../liveRegionAnnouncements";
import { preferredScrollBehavior } from "../motionPreferences";
import {
  evidenceMessageId,
  messageDomId,
  replyTargetMessageId,
  safeExternalUrl,
} from "../artifactEvidenceSources";
import "../styles/chat-timeline.css";
import { DirectorDecisionEvent } from "./DirectorDecisionEvent";

function navigateToTimelineMessage(messageId) {
  const target = globalThis.document?.getElementById(messageDomId(messageId));
  if (!target) return false;
  target.scrollIntoView({ behavior: preferredScrollBehavior(), block: "center" });
  target.focus({ preventScroll: true });
  return true;
}

export const ChatTimeline = memo(function ChatTimeline({
  messages = [],
  members = [],
  directorDecisions = [],
  typingMember = null,
  transientErrors = [],
  historyState = {},
  searchInput = "",
  searchState = {},
  onLoadOlder,
  onSearchInput,
  onSearch,
  onSearchMore,
  onClearSearch,
}) {
  const timelineRef = useRef(null);
  const bottomRef = useRef(null);
  const searchInputRef = useRef(null);
  const pinnedToBottomRef = useRef(true);
  const previousSearchQueryRef = useRef("");
  const announcementStateRef = useRef(null);
  const historyAnchorRef = useRef(null);
  const restoredHistoryRef = useRef(false);
  const actionRequestRef = useRef(false);
  const [hasNewBelow, setHasNewBelow] = useState(false);
  const [isAwayFromBottom, setIsAwayFromBottom] = useState(false);
  const [announcement, setAnnouncement] = useState(null);
  const [pendingAction, setPendingAction] = useState("");
  const [actionError, setActionError] = useState("");
  const memberIndex = useMemo(() => buildTimelineMemberIndex(members), [members]);
  const { memberMap } = memberIndex;
  const presentation = useMemo(
    () => buildChatTimelinePresentation({ messages, directorDecisions, searchState }),
    [directorDecisions, messages, searchState],
  );
  const transientErrorRows = useMemo(
    () => normalizeTransientErrors(transientErrors),
    [transientErrors],
  );
  const {
    loadedMessageIds,
    omittedRecordCount,
    projectionLimited,
    query,
    recordCount,
    searchActive,
    searchHasMore,
    searchLoading,
    searchMessageCount,
    sourceRecordCount,
    timelineItems,
    visibleMessages,
  } = presentation;
  const transientErrorSourceCount = Array.isArray(transientErrors) ? transientErrors.length : 0;
  const transientErrorsLimited = transientErrorSourceCount > CHAT_TIMELINE_TRANSIENT_ERROR_LIMIT;
  const controlledSearchInput = typeof searchInput === "string"
    ? searchInput.slice(0, 200)
    : "";
  const projectionNotes = [];
  if (omittedRecordCount) {
    projectionNotes.push(omittedRecordCount + " 条较早记录未进入本地排序窗口");
  }
  if (memberIndex.projectionLimited) {
    projectionNotes.push(
      (memberIndex.totalCount - CHAT_TIMELINE_MEMBER_LIMIT) + " 名成员未进入本地身份索引",
    );
  }
  if (transientErrorsLimited) {
    projectionNotes.push(
      (transientErrorSourceCount - CHAT_TIMELINE_TRANSIENT_ERROR_LIMIT)
        + " 条瞬态错误未展开",
    );
  }

  useEffect(() => {
    const transition = nextChatAnnouncementState(announcementStateRef.current, {
      messages: visibleMessages,
      roomId: historyState?.roomId,
      searchActive,
      historyLoading: historyState?.loading,
    });
    announcementStateRef.current = transition.next;
    if (transition.clear) setAnnouncement(null);
    else if (transition.announcement) setAnnouncement(transition.announcement);
  }, [historyState?.loading, historyState?.roomId, searchActive, visibleMessages]);

  useLayoutEffect(() => {
    const anchor = historyAnchorRef.current;
    if (!anchor) return;
    if (searchActive) {
      historyAnchorRef.current = null;
      return;
    }
    if (historyState?.loading) return;
    const element = timelineRef.current;
    if (!element) {
      historyAnchorRef.current = null;
      return;
    }
    element.scrollTop = anchor.scrollTop + (element.scrollHeight - anchor.scrollHeight);
    historyAnchorRef.current = null;
    restoredHistoryRef.current = true;
    pinnedToBottomRef.current = false;
    setIsAwayFromBottom(true);
  }, [historyState?.loading, searchActive, timelineItems.length]);

  useEffect(() => {
    if (searchActive) {
      if (previousSearchQueryRef.current !== query && timelineRef.current) {
        timelineRef.current.scrollTop = 0;
      }
      previousSearchQueryRef.current = query;
      pinnedToBottomRef.current = false;
      setHasNewBelow(false);
      setIsAwayFromBottom(false);
      return;
    }
    if (previousSearchQueryRef.current) {
      previousSearchQueryRef.current = "";
      pinnedToBottomRef.current = true;
      setIsAwayFromBottom(false);
    }
    if (restoredHistoryRef.current) {
      restoredHistoryRef.current = false;
      setHasNewBelow(false);
      setIsAwayFromBottom(true);
      return;
    }
    if (pinnedToBottomRef.current) {
      bottomRef.current?.scrollIntoView({ block: "end" });
      setHasNewBelow(false);
      setIsAwayFromBottom(false);
    } else {
      setHasNewBelow(true);
      setIsAwayFromBottom(true);
    }
  }, [
    query,
    searchActive,
    timelineItems.length,
    transientErrorRows.length,
    typingMember?.id,
  ]);

  const handleScroll = () => {
    const element = timelineRef.current;
    if (!element) return;
    const pinnedToBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
    if (pinnedToBottomRef.current !== pinnedToBottom) {
      pinnedToBottomRef.current = pinnedToBottom;
      setIsAwayFromBottom(!pinnedToBottom);
    }
    if (pinnedToBottom) setHasNewBelow(false);
  };

  const runTimelineAction = async (action, handler, fallback) => {
    if (actionRequestRef.current || typeof handler !== "function") return false;
    actionRequestRef.current = true;
    setPendingAction(action);
    setActionError("");
    try {
      await Promise.resolve(handler());
      return true;
    } catch (error) {
      setActionError(safeTimelineText(error?.message, 1000) || fallback);
      return false;
    } finally {
      actionRequestRef.current = false;
      setPendingAction((current) => current === action ? "" : current);
    }
  };

  const loadOlder = () => {
    if (
      !historyState?.hasMore
      || historyState?.loading
      || actionRequestRef.current
      || typeof onLoadOlder !== "function"
    ) return;
    const element = timelineRef.current;
    if (element) {
      historyAnchorRef.current = {
        scrollHeight: element.scrollHeight,
        scrollTop: element.scrollTop,
      };
      pinnedToBottomRef.current = false;
      setHasNewBelow(false);
      setIsAwayFromBottom(true);
    }
    void runTimelineAction("history", onLoadOlder, "加载更早消息失败。").then((completed) => {
      if (!completed) historyAnchorRef.current = null;
    });
  };

  const jumpToLatest = () => {
    const element = timelineRef.current;
    pinnedToBottomRef.current = true;
    if (element) {
      const previousScrollBehavior = element.style.scrollBehavior;
      element.style.scrollBehavior = "auto";
      element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
      element.focus({ preventScroll: true });
      const restoreScrollBehavior = () => {
        if (element.isConnected) element.style.scrollBehavior = previousScrollBehavior;
      };
      if (typeof globalThis.requestAnimationFrame === "function") {
        globalThis.requestAnimationFrame(restoreScrollBehavior);
      } else {
        restoreScrollBehavior();
      }
    } else {
      bottomRef.current?.scrollIntoView({ block: "end" });
    }
    setHasNewBelow(false);
    setIsAwayFromBottom(false);
  };

  const submitSearch = (event) => {
    event.preventDefault();
    if (
      !safeTimelineText(controlledSearchInput, 200)
      || searchLoading
      || actionRequestRef.current
      || typeof onSearch !== "function"
    ) return;
    void runTimelineAction("search", onSearch, "搜索消息失败。");
  };

  const clearSearch = () => {
    if (actionRequestRef.current || typeof onClearSearch !== "function") return;
    void runTimelineAction("clear-search", onClearSearch, "清除搜索失败。").then((completed) => {
      if (completed) searchInputRef.current?.focus();
    });
  };

  const loadMoreSearch = () => {
    if (
      !searchHasMore
      || searchLoading
      || actionRequestRef.current
      || typeof onSearchMore !== "function"
    ) return;
    void runTimelineAction("search-more", onSearchMore, "加载更多搜索结果失败。");
  };

  return (
    <div className="chat-timeline-wrap chat-timeline-workspace">
      <header className="chat-timeline-masthead">
        <span>
          <small>COLLABORATION LOG</small>
          <strong>讨论时间线</strong>
          <p>消息、主持决定与证据引用按确定性时间顺序排列。</p>
        </span>
        <em className={projectionLimited ? "is-limited" : ""}>
          <span>VISIBLE RECORDS</span>{recordCount}
        </em>
      </header>

      <div
        className="message-history-toolbar"
        data-history-terminal={!searchActive && !historyState?.hasMore ? "true" : "false"}
      >
        <div className="history-page-action">
          {searchActive ? (
            <span>“{query}” · {searchMessageCount} 条匹配消息</span>
          ) : (
            <button
              type="button"
              className="history-load-button"
              disabled={
                !historyState?.hasMore
                || historyState?.loading
                || pendingAction === "history"
                || typeof onLoadOlder !== "function"
              }
              aria-busy={historyState?.loading === true || pendingAction === "history"}
              onClick={loadOlder}
            >
              {historyState?.loading || pendingAction === "history"
                ? "正在加载…"
                : historyState?.hasMore
                  ? "加载更早消息"
                  : "已到最早记录"}
            </button>
          )}
        </div>
        <form className="message-history-search" role="search" onSubmit={submitSearch}>
          <Search size={14} aria-hidden="true" />
          <input
            ref={searchInputRef}
            type="search"
            value={controlledSearchInput}
            maxLength={200}
            placeholder="搜索消息内容"
            aria-label="搜索消息内容"
            aria-controls="chat-timeline-log"
            disabled={typeof onSearchInput !== "function"}
            onChange={(event) => onSearchInput?.(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Escape"
                && (controlledSearchInput || searchActive)
                && typeof onClearSearch === "function"
              ) {
                event.preventDefault();
                clearSearch();
              }
            }}
          />
          {(controlledSearchInput || searchActive) ? (
            <button
              type="button"
              className="history-clear-search"
              aria-label="清除消息搜索"
              title="清除搜索"
              disabled={pendingAction === "clear-search" || typeof onClearSearch !== "function"}
              aria-busy={pendingAction === "clear-search"}
              onClick={clearSearch}
            ><X size={13} aria-hidden="true" /></button>
          ) : null}
          <button
            type="submit"
            className="history-search-button"
            disabled={
              !safeTimelineText(controlledSearchInput, 200)
              || searchLoading
              || pendingAction === "search"
              || typeof onSearch !== "function"
            }
            aria-busy={searchLoading || pendingAction === "search"}
          >
            {pendingAction === "search" ? "搜索中…" : "搜索"}
          </button>
        </form>
      </div>

      <div className="timeline-status-stack">
        <div className="timeline-integrity-ledger" role="list" aria-label="时间线本地投影状态">
          <span role="listitem">
            <small>MODE</small>
            <strong>{searchActive ? "搜索视图" : "讨论视图"}</strong>
          </span>
          <span role="listitem">
            <small>PROJECTED / SOURCE</small>
            <strong>{recordCount} / {sourceRecordCount}</strong>
          </span>
          <span role="listitem">
            <small>MEMBER INDEX</small>
            <strong>{memberIndex.indexedCount} / {memberIndex.totalCount}</strong>
          </span>
        </div>
        {projectionNotes.length ? (
          <div className="timeline-projection-notice" role="note">
            <ShieldCheck size={14} aria-hidden="true" />
            <span><strong>本地投影上限已启用。</strong>{projectionNotes.join("；")}。</span>
          </div>
        ) : null}
        {actionError ? (
          <div className="message-history-error" role="alert">
            <AlertTriangle size={13} aria-hidden="true" />{actionError}
          </div>
        ) : null}
        {historyState?.error && !searchActive ? (
          <div className="message-history-error" role="alert">
            <AlertTriangle size={13} aria-hidden="true" />
            加载历史失败：{safeTimelineText(historyState.error, 1000) || "未知错误"}
          </div>
        ) : null}
        {searchState?.error ? (
          <div className="message-history-error" role="alert">
            <AlertTriangle size={13} aria-hidden="true" />
            搜索失败：{safeTimelineText(searchState.error, 1000) || "未知错误"}
          </div>
        ) : null}
        <div className="screen-reader-announcer" role="status" aria-live="polite" aria-atomic="true">
          {announcement ? <span key={announcement.id}>{announcement.text}</span> : null}
        </div>
      </div>

      <div
        id="chat-timeline-log"
        className="chat-timeline"
        ref={timelineRef}
        tabIndex={0}
        aria-label="讨论消息时间线"
        onScroll={handleScroll}
      >
        {searchActive && !searchLoading && recordCount === 0 ? (
          <div className="message-search-empty">没有找到包含“{query}”的消息。</div>
        ) : null}
        {!searchActive && recordCount === 0 && !typingMember && !transientErrorRows.length ? (
          <section className="conversation-empty-state" aria-label="开始协作">
            <div className="conversation-empty-mark" aria-hidden="true">
              <ShieldCheck size={21} strokeWidth={1.8} />
            </div>
            <p className="conversation-empty-eyebrow">本地证据工作台</p>
            <h2>从一个可核对的问题开始</h2>
            <p className="conversation-empty-copy">
              写下要核对的事实或决策，让成员带着来源回答。只有你确认后，系统才会启动一轮讨论。
            </p>
            <ol className="conversation-empty-route" aria-label="协作启动路径">
              <li>
                <span className="conversation-empty-route-icon" aria-hidden="true">
                  <Search size={14} />
                </span>
                <span><strong>明确问题</strong><small>写下事实或决策</small></span>
              </li>
              <li>
                <span className="conversation-empty-route-icon" aria-hidden="true">
                  <ShieldCheck size={14} />
                </span>
                <span><strong>核对证据</strong><small>让成员引用来源</small></span>
              </li>
              <li>
                <span className="conversation-empty-route-icon" aria-hidden="true">
                  <ArrowUpRight size={14} />
                </span>
                <span><strong>确认启动</strong><small>由你决定是否开轮</small></span>
              </li>
            </ol>
          </section>
        ) : null}

        {timelineItems.map((item) => {
          if (item.kind === "day") {
            return <div className="timeline-day-divider" role="separator" key={item.id}><span>{item.label}</span></div>;
          }
          if (item.kind === "director") {
            return <DirectorDecisionEvent decision={item.value} key={item.id} />;
          }
          const message = item.value;
          const messageId = safeTimelineText(message.id, 240);
          const messageElementId = messageId && item.duplicateIndex === 0
            ? messageDomId(messageId)
            : undefined;
          const isUser = message.sender_type === "user";
          const isSystem = message.sender_type === "system";
          const member = memberMap.get(safeTimelineText(message.sender_id, 240));
          const senderName = safeTimelineText(message.sender_name, 160)
            || safeTimelineText(member?.name, 160)
            || (isUser ? "我" : "未知成员");
          const identity = safeTimelineText(message.identity, 240);
          const content = safeTimelineText(message.content, CHAT_TIMELINE_CONTENT_LIMIT)
            || "（空消息）";
          const contentTruncated = typeof message.content === "string"
            && message.content.length > CHAT_TIMELINE_CONTENT_LIMIT;
          const replyMessageId = safeTimelineText(replyTargetMessageId(message), 240);
          const replyLabel = safeTimelineText(message.reply_to, 240) || "引用消息";
          const citationView = buildTimelineCitationPresentation(message.citations);
          const citations = citationView.citations;
          const provider = safeTimelineText(message.provider, 160);
          const model = safeTimelineText(message.model, 160);
          if (isSystem) {
            return (
              <div
                id={messageElementId}
                className="inline-error persisted timeline-system-event"
                tabIndex={-1}
                role="note"
                key={item.id}
              >
                <AlertTriangle size={13} aria-hidden="true" />
                <span>
                  {content}
                  {contentTruncated ? <small>消息过长，仅显示前 40,000 个字符。</small> : null}
                </span>
              </div>
            );
          }
          return (
            <article
              id={messageElementId}
              tabIndex={-1}
              key={item.id}
              className={"message " + (isUser ? "user" : "ai")}
              data-sender-type={isUser ? "user" : "member"}
              aria-label={senderName + " 的消息"}
            >
              {!isUser ? (
                <div
                  className="avatar"
                  style={{ background: safeAvatarColor(member?.avatar_color) }}
                  aria-hidden="true"
                >
                  {chatInitials(senderName)}
                </div>
              ) : null}
              <div className="message-body">
                <header>
                  <strong>{senderName}</strong>
                  {identity ? <span>{identity}</span> : null}
                  <time dateTime={safeTimelineText(message.created_at, 80) || undefined}>
                    {formatTimelineTime(message.created_at)}
                  </time>
                </header>
                <div className={"message-copy " + (isUser ? "user-bubble" : "")}>{content}</div>
                {contentTruncated ? (
                  <small className="message-content-limit">消息过长，仅显示前 40,000 个字符。</small>
                ) : null}
                {citations.length || citationView.hiddenCount ? (
                  <div className="message-citations" aria-label="消息证据引用">
                    {citations.map((citation) => {
                      const citedMessageId = safeTimelineText(evidenceMessageId(citation), 240);
                      const citedMessageLoaded = Boolean(
                        citedMessageId && loadedMessageIds.has(citedMessageId),
                      );
                      const sourceUrl = safeExternalUrl(
                        safeTimelineText(citation.source_url || citation.sourceUrl, 4000),
                      );
                      const citationTitle = safeTimelineText(citation.title, 300)
                        || safeTimelineText(citation.id, 240)
                        || "未命名来源";
                      const citationVersion = Number.isSafeInteger(citation.version)
                        && citation.version > 0 ? citation.version : 1;
                      const label = citedMessageId
                        ? "消息 · " + (safeTimelineText(citation.title) || citedMessageId)
                        : "资料 · " + citationTitle + " · v" + citationVersion;
                      const key = item.id + ":citation:" + citation.sourceIndex;
                      if (citedMessageLoaded) {
                        return (
                          <button
                            type="button"
                            key={key}
                            onClick={() => navigateToTimelineMessage(citedMessageId)}
                          >
                            {label}
                          </button>
                        );
                      }
                      if (sourceUrl) {
                        return (
                          <a key={key} href={sourceUrl} target="_blank" rel="noreferrer">
                            {label}<ArrowUpRight size={11} aria-hidden="true" />
                          </a>
                        );
                      }
                      return <span key={key}>{label}</span>;
                    })}
                    {citationView.hiddenCount ? (
                      <span className="citation-overflow-note">
                        另有 {citationView.hiddenCount} 条引用未展开
                      </span>
                    ) : null}
                  </div>
                ) : null}
                {message.reply_to && !isUser ? (
                  <div className="reply-line">
                    <Reply size={13} aria-hidden="true" />
                    {replyMessageId && loadedMessageIds.has(replyMessageId)
                      ? (
                          <button
                            type="button"
                            onClick={() => navigateToTimelineMessage(replyMessageId)}
                          >
                            回应 {replyLabel}
                          </button>
                        )
                      : <span>回应 {replyLabel}</span>}
                  </div>
                ) : null}
                {!isUser && provider ? (
                  <details className="message-audit">
                    <summary>执行记录</summary>
                    <span>
                      {provider} · {model || "默认模型"}
                      {" · 身份版本 v" + (
                        Number.isSafeInteger(message.member_version) && message.member_version > 0
                          ? message.member_version
                          : 1
                      )}
                      {message.turn_contract_version ? (
                        <> · 发言合同 {message.turn_contract_qualified ? "已核验" : "未通过"}</>
                      ) : null}
                    </span>
                  </details>
                ) : null}
              </div>
            </article>
          );
        })}

        {!searchActive && transientErrorRows.map((error) => (
          <div className="inline-error timeline-transient-error" role="alert" key={error.id}>
            <AlertTriangle size={13} aria-hidden="true" />{error.name} 未完成发言：{error.message}
          </div>
        ))}

        {!searchActive && typingMember ? (
          <article className="message ai typing-message" aria-label={(safeTimelineText(typingMember.name, 160) || "成员") + "正在输入"}>
            <div
              className="avatar"
              style={{ background: safeAvatarColor(typingMember.avatar_color) }}
              aria-hidden="true"
            >
              {chatInitials(safeTimelineText(typingMember.name, 160))}
            </div>
            <div className="message-body">
              <header>
                <strong>{safeTimelineText(typingMember.name, 160) || "成员"}</strong>
                {safeTimelineText(typingMember.identity, 240)
                  ? <span>{safeTimelineText(typingMember.identity, 240)}</span>
                  : null}
              </header>
              <div className="typing-indicator" aria-hidden="true"><i /><i /><i /></div>
            </div>
          </article>
        ) : null}

        {searchActive ? (
          <div className="message-search-more">
            <button
              type="button"
              disabled={
                !searchHasMore
                || searchLoading
                || pendingAction === "search-more"
                || typeof onSearchMore !== "function"
              }
              aria-busy={searchLoading || pendingAction === "search-more"}
              onClick={loadMoreSearch}
            >
              {searchLoading || pendingAction === "search-more"
                ? "正在搜索…"
                : searchHasMore
                  ? "加载更多搜索结果"
                  : "搜索结果已全部加载"}
            </button>
          </div>
        ) : null}
        <div ref={bottomRef} />
      </div>

      {!searchActive && isAwayFromBottom ? (
        <button
          className={hasNewBelow ? "new-message-button has-new" : "new-message-button return-only"}
          type="button"
          data-new-messages={hasNewBelow ? "true" : "false"}
          aria-label={hasNewBelow ? "有新消息，回到时间线底部" : "回到时间线底部"}
          onClick={jumpToLatest}
        >
          <ArrowDown size={14} strokeWidth={2} aria-hidden="true" />
          <span>{hasNewBelow ? "有新消息" : "回到最新"}</span>
        </button>
      ) : null}
    </div>
  );
});
