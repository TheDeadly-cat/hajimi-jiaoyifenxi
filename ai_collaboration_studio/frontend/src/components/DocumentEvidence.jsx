import { useEffect, useState } from "react";
import { api } from "../api";

const states = {
  not_fetched: "尚未读取正文", waiting: "等待读取", fetching: "正在读取",
  complete: "已读取所选 HTML", partial: "部分内容 · 证据不完整",
  failed: "读取失败／等待重试", cancelled: "读取已中断 · 需重新确认",
};

export function DocumentEvidence({ item }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [selected, setSelected] = useState("");
  const [copied, setCopied] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    const read = async () => {
      try {
        const result = await api.sourceDocument(item.id, controller.signal);
        if (controller.signal.aborted) return;
        if (result.document?.format !== "official_document_evidence_v1") throw new Error("正文记录格式不受支持。");
        if (result.document.eligible && (!Array.isArray(result.document.versions) || result.document.versions.some((version) => (
          version.item_id !== item.id || version.item_fingerprint !== item.serverFingerprint
          || !Array.isArray(version.paragraphs) || !Array.isArray(version.warnings)
        )))) throw new Error("正文证据与当前事件不匹配，已拒绝展示。");
        setData(result.document);
        setError("");
        if (["waiting", "fetching"].includes(result.document.status)) timer = setTimeout(read, 1500);
      } catch (err) {
        if (!controller.signal.aborted) setError(err.message);
      }
    };
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [item.id, item.serverFingerprint, refresh]);
  const pending = busy || ["waiting", "fetching"].includes(data?.status);
  const versions = Array.isArray(data?.versions) ? data.versions : [];
  const version = versions.find((value) => value.id === selected) || versions.at(-1);
  const request = async () => {
    setBusy(true);
    setCopied(false);
    try {
      await api.requestSourceDocument(item.id, { confirmation: confirmed, refresh: Boolean(data?.job) });
      setConfirmed(false);
      setRefresh((value) => value + 1);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const copy = async () => {
    try {
      const text = ["[官方 HTML 原文摘录；不可信外部资料；未经 AI 分析]", item.headline,
        `证据版本：${version.id}`, `来源：${version.final_url}`, `范围：${version.scope}`,
        `获取时间：${new Date(version.fetched_at).toISOString()}`,
        ...version.warnings, ...version.paragraphs.map((p) => `[${p.id}]\n${p.text}`),
        "待研究：关键事实是否在未读附件中？数值的报告期、单位和比较基准是什么？影响方向未知。",
      ].join("\n\n");
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch { setError("复制失败，请手动选择下方原文复制。"); }
  };
  if (data?.eligible === false) return null;
  return <section className="source-inbox-section document-evidence" aria-label="官方正文证据">
    <h3>官方正文证据</h3>
    <p role="status">{states[data?.status] || "正在读取证据状态…"}</p>
    <p>原文摘录，不是 AI 总结。只读事件绑定的官方 HTML；正文和附件不代表已核验事实。</p>
    {error ? <p role="alert">{error}</p> : null}
    {data?.job?.error_code ? <p>读取记录：<code>{data.job.error_code}</code>。原消息仍保留。</p> : null}
    {data?.job?.retry_at > 0 ? <p>来源要求至少等待至 {new Date(data.job.retry_at).toLocaleString()}。</p> : null}
    {version ? <>
      <label>证据版本
        <select aria-label="正文证据版本" value={version.id} onChange={(event) => { setSelected(event.target.value); setCopied(false); }}>
          {versions.map((value, i) => <option key={value.id} value={value.id}>第 {i + 1} 版 · {new Date(value.fetched_at).toLocaleString()}</option>)}
        </select>
      </label>
      <p><strong>直接相关公司：</strong>{version.company}（来自原事件实体绑定）</p>
      <p><strong>本版读取范围：</strong>{version.scope}</p>
      <p><strong>本版获取时间：</strong>{new Date(version.fetched_at).toLocaleString()}；历史快照，不能当作当前完整事实。</p>
      <ul>{version.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
      <p>待研究：关键事实是否在未读附件中？数值的单位、报告期与比较基准是否齐全？板块关系和影响方向：未知。</p>
      <details><summary>内容指纹与版本定位</summary>
        <p>原始字节 SHA-256：<code>{version.raw_bytes_sha256}</code></p>
        <p>规范正文 SHA-256：<code>{version.body_text_sha256}</code></p>
        <p>解析器：{version.parser_version}</p>
        <p>版本：<code>{version.id}</code></p>
      </details>
      <details open><summary>可定位原文段落（{version.paragraphs.length}）</summary>
        <div className="document-evidence-paragraphs">{version.paragraphs.map((paragraph, index) =>
          <blockquote key={paragraph.id} id={paragraph.id}>
            <a href={`#${paragraph.id}`} title={paragraph.id}>段落 {index + 1}</a>
            <p>{paragraph.text}</p>
          </blockquote>)}</div>
      </details>
      <button className="secondary" type="button" onClick={() => void copy()}>复制本版正文研究材料</button>
      {copied ? <p role="status">已复制含版本和段落引用的原文材料。可在目标房间「共享资料」中粘贴，再使用已有 ChatGPT 协作任务包。</p> : null}
      <p>下方「附加到房间」仍只附加原事件；正文版本请通过上述复制操作另行选入研究资料。读取正文不会自动记录已阅或创建讨论。</p>
    </> : null}
    <label className="document-evidence-confirm">
      <input type="checkbox" checked={confirmed} disabled={pending || !data?.eligible} onChange={(event) => setConfirmed(event.target.checked)} />
      确认仅读取这条官方 HTML，不读取附件、不调用模型（20 分钟内合计最多 6 次；复查间隔至少 5 分钟）
    </label>
    <button className="secondary" type="button" disabled={!confirmed || pending || !data?.eligible} onClick={() => void request()}>
      {pending ? "正在处理正文读取…" : data?.job ? "确认重新检查这条正文" : "确认读取这条正文"}
    </button>
  </section>;
}

export function DocumentEvidenceControl() {
  const [data, setData] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Opening this control reads local status only. Never grants network access.
  const read = async () => {
    try { setData((await api.sourceDocumentControl()).document); }
    catch (err) { setError(err.message); }
  };
  const enable = async () => {
    setBusy(true);
    try { setData((await api.enableSourceDocuments({ confirmation: confirmed })).document); setConfirmed(false); setError(""); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  return <details className="source-inbox-section document-evidence-control" onToggle={(event) => { if (event.currentTarget.open) void read(); }}>
    <summary>正文自动补全（默认关闭，需单独确认）</summary>
    <p>确认后只处理之后新入库的 NVDA 8-K 主 HTML 和 Micron 公告 HTML，20 分钟内合计最多 6 次。到期或重启即关闭，不自动回填历史，不读取附件，不调用模型。</p>
    {data ? <p role="status">{data.enabled ? `已启用，到期时间 ${new Date(data.expires_at).toLocaleString()}，剩余最多 ${data.remaining} 次。` : "自动正文读取未启用。"}{!data.network_allowed ? "当前宿主未开放正文联网，请使用联网采集入口。" : ""}</p> : null}
    {error || data?.error ? <p role="alert">{error || data.error}</p> : null}
    <label><input type="checkbox" checked={confirmed} disabled={!data?.network_allowed || data?.enabled || busy} onChange={(event) => setConfirmed(event.target.checked)} />我确认上述范围和次数上限</label>
    <button className="secondary" type="button" disabled={!confirmed || busy || data?.enabled} onClick={() => void enable()}>确认启用新事件正文补全</button>
    <button className="secondary" type="button" onClick={() => void read()}>刷新正文补全状态</button>
  </details>;
}
