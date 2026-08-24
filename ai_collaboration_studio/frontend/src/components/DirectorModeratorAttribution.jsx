import { Bot, ShieldCheck } from "lucide-react";
import { memo } from "react";
import { directorModeratorAttribution } from "../directorDecision";
import "../styles/director-moderator-attribution-polish.css";

export const DirectorModeratorAttribution = memo(function DirectorModeratorAttribution({ context }) {
  const attribution = directorModeratorAttribution(context);
  if (!attribution.available) {
    return (
      <div className="director-moderator-attribution legacy" role="note">
        <ShieldCheck aria-hidden="true" size={13} />
        <span>{attribution.notice}</span>
      </div>
    );
  }

  const usageClass = attribution.modelUsageKnown
    ? attribution.modelUsed ? "model-used" : "model-not-used"
    : "model-usage-unknown";
  const versionLabel = attribution.memberVersion ? `v${attribution.memberVersion}` : "版本未记录";
  const routeLabel = attribution.modelUsageKnown
    ? attribution.modelUsed ? "实际调用" : "冻结路由"
    : "模型路由";
  const providerLabel = attribution.provider || "执行器未记录";
  const modelLabel = attribution.model || "模型未记录";
  const discussionModeLabel = attribution.discussionModeLabel || "讨论模式未记录";

  return (
    <div
      className={`director-moderator-attribution ${usageClass}${attribution.complete ? "" : " incomplete"}`}
      role="note"
      aria-label={`主持归因：${attribution.memberName}，${versionLabel}，${attribution.authorityLabel}，${routeLabel} ${providerLabel} / ${modelLabel}，${discussionModeLabel}`}
    >
      <div className="director-moderator-head">
        <Bot aria-hidden="true" size={13} />
        <span>主持</span>
        <strong>{attribution.memberName}</strong>
        <em>{versionLabel}</em>
        <b>{attribution.authorityLabel}</b>
      </div>
      {attribution.identity ? <p>{attribution.identity}</p> : null}
      <div className="director-moderator-route">
        <span>{routeLabel}</span>
        <code>{providerLabel}</code>
        <i aria-hidden="true">·</i>
        <code>{modelLabel}</code>
        <small>{discussionModeLabel}</small>
      </div>
      {attribution.notice ? <small className="director-moderator-notice">{attribution.notice}</small> : null}
    </div>
  );
});
