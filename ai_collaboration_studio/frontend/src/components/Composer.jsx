import { AtSign, Send, Sparkles } from "lucide-react";
import { useId, useRef, useState } from "react";
import "../styles/composer-polish.css";

export function Composer({
  value,
  onChange,
  onMention,
  onSend,
  onStartRound,
  onStartChatGPT,
  disabled,
  roundDisabled = false,
  roundStatusLabel = "",
  roundStatusWarning = false,
  roundStatusTitle = "",
  chatGPTDisabled = false,
  chatGPTStatusTitle = "",
  members,
}) {
  const [mentionOpen, setMentionOpen] = useState(false);
  const textareaRef = useRef(null);
  const mentionButtonRef = useRef(null);
  const mentionMenuRef = useRef(null);
  const mentionMenuId = useId();
  const roundStatusId = useId();
  const submit = () => {
    if (!value.trim() || disabled) return;
    onSend();
  };
  const mention = (member) => {
    const spacer = value && !value.endsWith(" ") ? " " : "";
    onChange(`${value}${spacer}@${member.name} `);
    onMention?.(member);
    setMentionOpen(false);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };
  const focusMentionItem = (index) => {
    const items = Array.from(mentionMenuRef.current?.querySelectorAll('[role="menuitem"]') || []);
    if (!items.length) return;
    const nextIndex = ((index % items.length) + items.length) % items.length;
    items[nextIndex]?.focus();
  };
  const handleMentionTriggerKeyDown = (event) => {
    if (mentionOpen || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
    event.preventDefault();
    setMentionOpen(true);
    requestAnimationFrame(() => focusMentionItem(event.key === "ArrowUp" ? -1 : 0));
  };
  const handleMentionKeyDown = (event) => {
    if (!mentionOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setMentionOpen(false);
      requestAnimationFrame(() => mentionButtonRef.current?.focus());
      return;
    }
    if (
      ["Enter", " "].includes(event.key)
      && event.target.getAttribute("role") === "menuitem"
    ) {
      event.preventDefault();
      event.stopPropagation();
      event.target.click();
      return;
    }
    const items = Array.from(mentionMenuRef.current?.querySelectorAll('[role="menuitem"]') || []);
    if (!items.length) return;
    const currentIndex = items.indexOf(document.activeElement);
    const nextIndex = {
      ArrowDown: currentIndex < 0 ? 0 : currentIndex + 1,
      ArrowUp: currentIndex < 0 ? items.length - 1 : currentIndex - 1,
      Home: 0,
      End: items.length - 1,
    }[event.key];
    if (nextIndex === undefined) return;
    event.preventDefault();
    event.stopPropagation();
    focusMentionItem(nextIndex);
  };
  return (
    <div className="composer">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            submit();
          }
        }}
        placeholder="输入消息，@成员或发起新一轮…"
        aria-label="消息内容"
        aria-describedby={roundStatusLabel ? roundStatusId : undefined}
        disabled={disabled}
      />
      <div className="composer-toolbar">
        <div
          className="mention-control"
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setMentionOpen(false);
          }}
          onKeyDown={handleMentionKeyDown}
        >
          <button
            ref={mentionButtonRef}
            className="icon-button"
            title="提及成员"
            aria-label="提及成员"
            aria-haspopup="menu"
            aria-expanded={mentionOpen}
            aria-controls={mentionOpen ? mentionMenuId : undefined}
            type="button"
            onClick={() => setMentionOpen((open) => !open)}
            onKeyDown={handleMentionTriggerKeyDown}
            disabled={disabled}
          ><AtSign size={18} /></button>
          {mentionOpen && (
            <div ref={mentionMenuRef} id={mentionMenuId} className="mention-menu" role="menu" aria-label="选择提及成员">
              {members.filter((member) => member.enabled).map((member, index) => (
                <button key={member.id} type="button" role="menuitem" tabIndex={index === 0 ? 0 : -1} onClick={() => mention(member)}>
                  <span style={{ background: member.avatar_color }}>{member.name.slice(0, 1)}</span>
                  <div><strong>{member.name}</strong><small>{member.identity}</small></div>
                </button>
              ))}
            </div>
          )}
        </div>
        <span className="composer-keyboard-hint" aria-hidden="true">
          <kbd>Enter</kbd> 发送 <i /> <kbd>Shift + Enter</kbd> 换行
        </span>
        {roundStatusLabel ? (
          <span
            id={roundStatusId}
            className={roundStatusWarning ? "composer-provider-summary warning" : "composer-provider-summary"}
            title={roundStatusTitle || roundStatusLabel}
          >
            {roundStatusLabel}
          </span>
        ) : null}
        <div className="composer-actions">
          <button
            className="secondary"
            type="button"
            onClick={(event) => onStartRound?.(event.currentTarget)}
            disabled={disabled || roundDisabled}
          >
            <Sparkles size={16} />API 轮次
          </button>
          <button
            className="primary chatgpt-button"
            type="button"
            onClick={(event) => onStartChatGPT?.(event.currentTarget)}
            disabled={disabled || chatGPTDisabled}
            title={chatGPTStatusTitle || "打开人工 ChatGPT 协作席位；可在弹窗中填写研究问题，不会自动调用 Provider。"}
            aria-label="打开人工 ChatGPT 协作席位"
          >
            <Sparkles size={16} />ChatGPT 协作
          </button>
          <button className="secondary send-button" type="button" onClick={submit} disabled={disabled || !value.trim()}>
            <Send size={16} />发送
          </button>
        </div>
      </div>
    </div>
  );
}
