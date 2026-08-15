import { AtSign, Send, Sparkles } from "lucide-react";
import { useState } from "react";

export function Composer({
  value,
  onChange,
  onMention,
  onSend,
  onStartRound,
  disabled,
  roundDisabled = false,
  roundStatusLabel = "",
  roundStatusWarning = false,
  roundStatusTitle = "",
  members,
}) {
  const [mentionOpen, setMentionOpen] = useState(false);
  const submit = () => {
    if (!value.trim() || disabled) return;
    onSend();
  };
  const mention = (member) => {
    const spacer = value && !value.endsWith(" ") ? " " : "";
    onChange(`${value}${spacer}@${member.name} `);
    onMention?.(member);
    setMentionOpen(false);
  };
  return (
    <div className="composer">
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            submit();
          }
        }}
        placeholder="输入消息，@成员或发起新一轮…"
        disabled={disabled}
      />
      <div className="composer-toolbar">
        <div className="mention-control">
          <button
            className="icon-button"
            title="提及成员"
            aria-label="提及成员"
            aria-expanded={mentionOpen}
            type="button"
            onClick={() => setMentionOpen((open) => !open)}
            disabled={disabled}
          ><AtSign size={18} /></button>
          {mentionOpen && (
            <div className="mention-menu" role="menu">
              {members.filter((member) => member.enabled).map((member) => (
                <button key={member.id} type="button" role="menuitem" onClick={() => mention(member)}>
                  <span style={{ background: member.avatar_color }}>{member.name.slice(0, 1)}</span>
                  <div><strong>{member.name}</strong><small>{member.identity}</small></div>
                </button>
              ))}
            </div>
          )}
        </div>
        {roundStatusLabel ? (
          <span
            className={roundStatusWarning ? "composer-provider-summary warning" : "composer-provider-summary"}
            title={roundStatusTitle || roundStatusLabel}
          >
            {roundStatusLabel}
          </span>
        ) : null}
        <div className="composer-actions">
          <button
            className="secondary"
            onClick={(event) => onStartRound?.(event.currentTarget)}
            disabled={disabled || roundDisabled}
          >
            <Sparkles size={16} />开始一轮
          </button>
          <button className="primary send-button" onClick={submit} disabled={disabled || !value.trim()}>
            <Send size={16} />发送
          </button>
        </div>
      </div>
    </div>
  );
}
