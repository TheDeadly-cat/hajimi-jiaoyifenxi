import { ArrowUpRight, Reply, Search, ShieldCheck, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { nextChatAnnouncementState } from "../liveRegionAnnouncements";
import { preferredScrollBehavior } from "../motionPreferences";
import {
  evidenceMessageId,
  messageDomId,
  replyTargetMessageId,
  safeExternalUrl,
} from "../artifactEvidenceSources";
import { DirectorDecisionEvent } from "./DirectorDecisionEvent";

function initials(name = "AI") {
  return name === "我" ? "我" : name.slice(0, 2);
}

function messageTime(timestamp) {
  return new Date(timestamp).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function timelineTimestamp(value) {
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function navigateToTimelineMessage(messageId) {
  const target = globalThis.document?.getElementById(messageDomId(messageId));
  if (!target) return false;
  target.scrollIntoView({ behavior: preferredScrollBehavior(), block: "center" });
  target.focus({ preventScroll: true });
  return true;
}

export function ChatTimeline({
  messages,
  members,
  directorDecisions,
  typingMember,
  transientErrors,
  historyState,
  searchInput,
  searchState,
  onLoadOlder,
  onSearchInput,
  onSearch,
  onSearchMore,
  onClearSearch,
}) {
  const timelineRef = useRef(null);
  const bottomRef = useRef(null);
  const pinnedToBottomRef = useRef(true);
  const previousSearchQueryRef = useRef("");
  const announcementStateRef = useRef(null);
  const [hasNewBelow, setHasNewBelow] = useState(false);
  const [announcement, setAnnouncement] = useState(null);
  const memberMap = useMemo(
    () => new Map((members || []).map((member) => [member.id, member])),
    [members],
  );
  const searchActive = Boolean(searchState?.query);
  const visibleMessages = searchActive ? searchState.messages : messages;
  const visibleDirectorDecisions = searchActive
    ? searchState.directorDecisions
    : directorDecisions;
  const loadedMessageIds = useMemo(
    () => new Set((visibleMessages || []).map((message) => String(message.id || ""))),
    [visibleMessages],
  );
  const timelineItems = useMemo(() => [
    ...(visibleMessages || []).map((message) => ({
      id: `message:${message.id}`,
      kind: "message",
      timestamp: timelineTimestamp(message.created_at),
      sequence: 0,
      value: message,
    })),
    ...(visibleDirectorDecisions || []).map((decision) => ({
      id: `director:${decision.id}`,
      kind: "director",
      timestamp: timelineTimestamp(decision.created_at),
      sequence: Number(decision.sequence_no) || 0,
      value: decision,
    })),
  ].sort((left, right) => (
    left.timestamp - right.timestamp
    || (left.kind === right.kind ? left.sequence - right.sequence : left.kind === "director" ? -1 : 1)
    || left.id.localeCompare(right.id)
  )), [visibleDirectorDecisions, visibleMessages]);
  useEffect(() => {
    const transition = nextChatAnnouncementState(announcementStateRef.current, {
      messages,
      roomId: historyState?.roomId,
      searchActive,
      historyLoading: historyState?.loading,
    });
    announcementStateRef.current = transition.next;
    if (transition.clear) setAnnouncement(null);
    else if (transition.announcement) setAnnouncement(transition.announcement);
  }, [historyState?.loading, historyState?.roomId, messages, searchActive]);
  useEffect(() => {
    const activeQuery = String(searchState?.query || "");
    if (searchActive) {
      if (previousSearchQueryRef.current !== activeQuery && timelineRef.current) {
        timelineRef.current.scrollTop = 0;
      }
      previousSearchQueryRef.current = activeQuery;
      pinnedToBottomRef.current = false;
      setHasNewBelow(false);
      return;
    }
    if (previousSearchQueryRef.current) {
      previousSearchQueryRef.current = "";
      pinnedToBottomRef.current = true;
    }
    if (pinnedToBottomRef.current) {
      bottomRef.current?.scrollIntoView({ block: "end" });
      setHasNewBelow(false);
    } else {
      setHasNewBelow(true);
    }
  }, [searchActive, searchState?.query, timelineItems.length, typingMember?.id, transientErrors.length]);
  const handleScroll = () => {
    const element = timelineRef.current;
    if (!element) return;
    pinnedToBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
    if (pinnedToBottomRef.current) setHasNewBelow(false);
  };
  const jumpToLatest = () => {
    pinnedToBottomRef.current = true;
    bottomRef.current?.scrollIntoView({
      block: "end",
      behavior: preferredScrollBehavior(),
    });
    setHasNewBelow(false);
  };
  const submitSearch = (event) => {
    event.preventDefault();
    onSearch?.();
  };

  return (
    <div className="chat-timeline-wrap">
      <div className="message-history-toolbar">
        <div className="history-page-action">
          {searchActive ? (
            <span>“{searchState.query}” · {searchState.messages.length} 条匹配消息</span>
          ) : (
            <button
              type="button"
              className="history-load-button"
              disabled={!historyState?.hasMore || historyState?.loading}
              onClick={onLoadOlder}
            >
              {historyState?.loading
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
            type="search"
            value={searchInput || ""}
            maxLength={200}
            placeholder="搜索消息内容"
            aria-label="搜索消息内容"
            onChange={(event) => onSearchInput?.(event.target.value)}
          />
          {(searchInput || searchActive) ? (
            <button
              type="button"
              className="history-clear-search"
              aria-label="清除消息搜索"
              title="清除搜索"
              onClick={onClearSearch}
            ><X size={13} /></button>
          ) : null}
          <button type="submit" className="history-search-button" disabled={!String(searchInput || "").trim()}>
            搜索
          </button>
        </form>
      </div>
      {historyState?.error && !searchActive ? (
        <div className="message-history-error">加载历史失败：{historyState.error}</div>
      ) : null}
      {searchState?.error ? (
        <div className="message-history-error">搜索失败：{searchState.error}</div>
      ) : null}
      <div
        className="screen-reader-announcer"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {announcement ? <span key={announcement.id}>{announcement.text}</span> : null}
      </div>
      <div className="chat-timeline" ref={timelineRef} onScroll={handleScroll}>
      {searchActive && !searchState.loading && timelineItems.length === 0 ? (
        <div className="message-search-empty">没有找到包含“{searchState.query}”的消息。</div>
      ) : null}
      {!searchActive && timelineItems.length === 0 && !typingMember && !transientErrors.length ? (
        <section className="conversation-empty-state" aria-label="开始协作">
          <div className="conversation-empty-mark" aria-hidden="true">
            <ShieldCheck size={21} strokeWidth={1.8} />
          </div>
          <p className="conversation-empty-eyebrow">本地证据工作台</p>
          <h2>从一个清晰的问题开始</h2>
          <p className="conversation-empty-copy">
            消息、成员回合和证据链会在这里按时间展开。先写下你想核对的事实，再决定是否启动一轮讨论。
          </p>
          <div className="conversation-empty-hints">
            <span><ShieldCheck size={13} />证据优先</span>
            <span><ArrowUpRight size={13} />用户确认后执行</span>
          </div>
        </section>
      ) : null}
      {timelineItems.map((item) => {
        if (item.kind === "director") {
          return <DirectorDecisionEvent decision={item.value} key={item.id} />;
        }
        const message = item.value;
        const isUser = message.sender_type === "user";
        const isSystem = message.sender_type === "system";
        const member = memberMap.get(message.sender_id);
        const replyMessageId = replyTargetMessageId(message);
        if (isSystem) {
          return <div id={messageDomId(message.id)} className="inline-error persisted" tabIndex={-1} key={item.id}>{message.content}</div>;
        }
        return (
          <article id={messageDomId(message.id)} tabIndex={-1} key={item.id} className={isUser ? "message user" : "message ai"}>
            {!isUser && (
              <div className="avatar" style={{ background: member?.avatar_color || "#4f6b8a" }}>
                {initials(message.sender_name)}
              </div>
            )}
            <div className="message-body">
              <header>
                <strong>{message.sender_name}</strong>
                {message.identity && <span>{message.identity}</span>}
                <time>{messageTime(message.created_at)}</time>
              </header>
              <div className={isUser ? "message-copy user-bubble" : "message-copy"}>{message.content}</div>
              {message.citations?.length > 0 ? (
                <div className="message-citations">
                  {message.citations.map((citation, index) => {
                    const citedMessageId = evidenceMessageId(citation);
                    const citedMessageLoaded = Boolean(citedMessageId && loadedMessageIds.has(citedMessageId));
                    const sourceUrl = safeExternalUrl(citation.source_url || citation.sourceUrl);
                    const label = citedMessageId
                      ? `消息 · ${citation.title || citedMessageId}`
                      : `资料 · ${citation.title || citation.id} · v${citation.version || 1}`;
                    const key = citation.id || citation.source_id || `${message.id}:${index}`;
                    if (citedMessageLoaded) {
                      return <button type="button" key={key} onClick={() => navigateToTimelineMessage(citedMessageId)}>{label}</button>;
                    }
                    if (sourceUrl) {
                      return <a key={key} href={sourceUrl} target="_blank" rel="noreferrer">{label}</a>;
                    }
                    return <span key={key}>{label}</span>;
                  })}
                </div>
              ) : null}
              {message.reply_to && !isUser && (
                <div className="reply-line">
                  <Reply size={13} />
                  {replyMessageId && loadedMessageIds.has(replyMessageId)
                    ? <button type="button" onClick={() => navigateToTimelineMessage(replyMessageId)}>回应 {message.reply_to}</button>
                    : <span>回应 {message.reply_to}</span>}
                </div>
              )}
              {!isUser && message.provider ? <details className="message-audit">
                <summary>执行记录</summary>
                <span>
                  {message.provider} · {message.model || "默认模型"} · 身份版本 v{message.member_version || 1}
                  {message.turn_contract_version ? (
                    <> · 发言合同 {message.turn_contract_qualified ? "已核验" : "未通过"}</>
                  ) : null}
                </span>
              </details> : null}
            </div>
          </article>
        );
      })}
      {!searchActive && transientErrors.map((error) => (
        <div className="inline-error" key={error.id}>{error.name} 未完成发言：{error.message}</div>
      ))}
      {!searchActive && typingMember && (
        <article className="message ai typing-message">
          <div className="avatar" style={{ background: typingMember.avatar_color }}>{initials(typingMember.name)}</div>
          <div className="message-body">
            <header><strong>{typingMember.name}</strong><span>{typingMember.identity}</span></header>
            <div className="typing-indicator" aria-label={`${typingMember.name}正在输入`}><i /><i /><i /></div>
          </div>
        </article>
      )}
      {searchActive ? (
        <div className="message-search-more">
          <button
            type="button"
            disabled={!searchState.hasMore || searchState.loading}
            onClick={onSearchMore}
          >
            {searchState.loading
              ? "正在搜索…"
              : searchState.hasMore
                ? "加载更多搜索结果"
                : "搜索结果已全部加载"}
          </button>
        </div>
      ) : null}
        <div ref={bottomRef} />
      </div>
      {hasNewBelow ? <button className="new-message-button" type="button" onClick={jumpToLatest}>有新消息 · 回到底部</button> : null}
    </div>
  );
}
