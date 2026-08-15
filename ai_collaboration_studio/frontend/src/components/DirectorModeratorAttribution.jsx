import { Bot, ShieldCheck } from "lucide-react";
import { directorModeratorAttribution } from "../directorDecision";

export function DirectorModeratorAttribution({ context }) {
  const attribution = directorModeratorAttribution(context);
  if (!attribution.available) {
    return (
      <div className="director-moderator-attribution legacy">
        <ShieldCheck size={13} />
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

  return (
    <div
      className={`director-moderator-attribution ${usageClass}${attribution.complete ? "" : " incomplete"}`}
      aria-label={`主持归因：${attribution.memberName}，${versionLabel}，${attribution.authorityLabel}`}
    >
      <div className="director-moderator-head">
        <Bot size={13} />
        <span>主持</span>
        <strong>{attribution.memberName}</strong>
        <em>{versionLabel}</em>
        <b>{attribution.authorityLabel}</b>
      </div>
      {attribution.identity ? <p title={attribution.identity}>{attribution.identity}</p> : null}
      <div className="director-moderator-route">
        <span>{routeLabel}</span>
        <code>{attribution.provider}</code>
        <i>·</i>
        <code>{attribution.model}</code>
        <small>{attribution.discussionModeLabel}</small>
      </div>
      {attribution.notice ? <small className="director-moderator-notice">{attribution.notice}</small> : null}
    </div>
  );
}
