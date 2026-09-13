import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import "../styles/document-selection.css";

export function DocumentSelection({ item, version, rooms, roomId, onRoomChange }) {
  const [paragraphIds, setParagraphIds] = useState([]);
  const [preview, setPreview] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(null);
  const requestRef = useRef(null);
  const signature = JSON.stringify([item.id, item.stateVersion, version.id, roomId, paragraphIds]);
  const validPreview = preview?.signature === signature ? preview.data : null;
  useEffect(() => {
    setPreview(null); setConfirmed(false); setSaved(null); setError(""); setBusy(false);
    return () => requestRef.current?.abort();
  }, [signature]);
  const invalidate = () => {
    requestRef.current?.abort(); setPreview(null); setConfirmed(false); setSaved(null); setError("");
  };
  const payload = { document_version_id: version.id, paragraph_ids: paragraphIds,
    room_id: roomId, expected_state_version: item.stateVersion };
  const inspect = async () => {
    invalidate();
    const controller = new AbortController(); requestRef.current = controller;
    setBusy(true);
    try {
      const result = await api.previewDocumentSelection(item.id, payload, controller.signal);
      if (!controller.signal.aborted) setPreview({ signature, data: result.selection });
    } catch (err) { if (!controller.signal.aborted) setError(err.message); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  };
  const save = async () => {
    if (!validPreview || !confirmed) return;
    const controller = new AbortController(); requestRef.current = controller;
    setBusy(true); setError("");
    try {
      const result = await api.saveDocumentSelection(item.id, { ...payload, confirmation: true,
        preview_sha256: validPreview.preview_sha256 }, controller.signal);
      if (!controller.signal.aborted) { setSaved(result); setConfirmed(false); }
    } catch (err) { if (!controller.signal.aborted) setError(err.message); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  };
  return <section className="document-selection" aria-label="正文选段加入研究">
    <h4>将本版选段加入研究房间</h4>
    <p>逐段选择，保留本版来源与引用。未选段落和所有附件不会进入这份材料。</p>
    <fieldset disabled={busy}>
      <legend>选择正文段落（已选 {paragraphIds.length} / {version.paragraphs.length} 段）</legend>
      <div className="document-selection-options">{version.paragraphs.map((paragraph, index) =>
        <label key={paragraph.id}>
          <input type="checkbox" aria-label={`选择段落 ${index + 1}`} checked={paragraphIds.includes(paragraph.id)}
            onChange={(event) => { invalidate(); setParagraphIds((ids) => event.target.checked ? [...ids, paragraph.id] : ids.filter((id) => id !== paragraph.id)); }} />
          <span><strong>段落 {index + 1}</strong><span className="document-selection-text">{paragraph.text}</span></span>
        </label>)}</div>
      <label className="document-selection-room">研究房间
        <select aria-label="正文选段目标房间" value={roomId} onChange={(event) => { invalidate(); onRoomChange(event.target.value); }}>
          <option value="">请选择目标房间</option>
          {rooms.map((room) => <option key={room.id} value={room.id}>{room.title}</option>)}
        </select>
      </label>
    </fieldset>
    <button className="secondary" type="button" disabled={busy || !paragraphIds.length || !roomId} onClick={() => void inspect()}>预览选段与研究包范围</button>
    {error ? <p role="alert">{error}</p> : null}
    {validPreview ? <section className="document-selection-preview" aria-label="将加入的研究内容">
      <h4>将加入：{validPreview.room_title}</h4>
      <dl>
        <div><dt>原文 / 已选 / 未选</dt><dd>{validPreview.total_paragraphs} / {validPreview.selected_paragraphs} / {validPreview.omitted_paragraphs} 段</dd></div>
        <div><dt>选段原文</dt><dd>{validPreview.selected_characters} 字符</dd></div>
        <div><dt>含来源、引用和警告</dt><dd>{validPreview.packaged_characters} 字符</dd></div>
        <div><dt>研究包将包含</dt><dd>{validPreview.package_characters} / {validPreview.excerpt_limit} 字符</dd></div>
      </dl>
      <p>正文版本：<code>{validPreview.document_version_id}</code></p>
      <p>官方来源：<span>{validPreview.source_url}</span></p>
      {!validPreview.fits ? <p role="alert">超过 1,600 字符边界，当前不会加入。请减少所选段落后重新预览；不会自动截断或拆分。</p> : null}
      {!validPreview.acknowledged ? <p role="alert">请先在下方勾选“已阅，不代表事实确认”并点击“记录已阅”，然后重新预览。</p> : null}
      <details open><summary>{validPreview.fits ? "研究包中的完整选段文本" : "当前包装文本（超限，未加入）"}</summary>
        <pre>{validPreview.content}</pre>
      </details>
      <p>整间房间的资料还会在 ChatGPT 协作冻结前再次预览；本次只保存这份选段材料。</p>
      <label className="document-selection-confirm"><input type="checkbox" checked={confirmed} disabled={busy || !validPreview.can_save}
        onChange={(event) => setConfirmed(event.target.checked)} />我确认以上房间、版本和选段范围；资料未核验，不启动模型或讨论。</label>
      <button className="primary" type="button" disabled={busy || !confirmed || !validPreview.can_save} onClick={() => void save()}>确认加入研究房间</button>
    </section> : null}
    {saved ? <p role="status">{saved.idempotent_replay ? "这份选段已存在，未重复创建。" : "选段已保存。"}{saved.material.active ? "可在目标房间的共享资料中查看，再打开 ChatGPT 协作预览并冻结任务包。" : "已有资料当前已停用，没有自动启用。"}</p> : null}
  </section>;
}
