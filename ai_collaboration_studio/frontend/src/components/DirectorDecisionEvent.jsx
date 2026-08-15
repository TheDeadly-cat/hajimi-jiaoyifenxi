import { GitBranch } from "lucide-react";
import { directorSourceLabel } from "../directorDecision";
import { stageLabel } from "../workflowPolicy";
import { DirectorModeratorAttribution } from "./DirectorModeratorAttribution";

function decisionTime(timestamp) {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function workspaceFocusTitle(workspaceFocus) {
  if (typeof workspaceFocus === "string") return workspaceFocus;
  return workspaceFocus?.title || "";
}

export function DirectorDecisionEvent({ decision }) {
  const finish = decision.action === "finish";
  const memberName = decision.member_name || "待定成员";
  const focusTitle = workspaceFocusTitle(decision.workspace_focus);
  const source = directorSourceLabel(decision.source);
  const stage = stageLabel(decision.stage || "flexible");
  const time = decisionTime(decision.created_at);

  return (
    <article
      className={finish ? "director-trace-event finish" : "director-trace-event speak"}
      aria-label={finish ? "主持建议结束，等待用户复核" : `主持人选择${memberName}发言`}
    >
      <div className="director-trace-head">
        <span className="director-trace-icon"><GitBranch size={14} /></span>
        <strong>{finish ? "主持建议结束，等待用户复核" : `主持人 → ${memberName}`}</strong>
        {time ? <time>{time}</time> : null}
      </div>
      {focusTitle ? <div className="director-trace-focus"><span>当前焦点</span><strong>{focusTitle}</strong></div> : null}
      <DirectorModeratorAttribution context={decision.moderator_context} />
      <details className="director-trace-details">
        <summary>查看调度依据</summary>
        <p>{decision.reason || "主持人未提供补充说明。"}</p>
        <div className="director-trace-meta">
          <span>{source}</span>
          <span>{stage}</span>
          {Number(decision.sequence_no) > 0 ? <span>第 {Number(decision.sequence_no)} 次调度</span> : null}
        </div>
      </details>
    </article>
  );
}
