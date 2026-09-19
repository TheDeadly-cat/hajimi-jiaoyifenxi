"""Local masked input; no credentials in files, command arguments, or logs."""
from __future__ import annotations

from pathlib import Path
import time
import uuid


def acceptable_password(value: str) -> bool:
    return bool(value) and len(value) <= 4096 and all(32 < ord(char) < 127 for char in value)


def ask_api_key(plan: dict, status_directory: Path) -> str:
    import tkinter as tk
    from tkinter import ttk
    from backend.api_pilot import write_record

    config = plan["config"]
    max_calls = plan.get("max_calls", 1)
    if time.time() * 1000 >= config["expires_at_ms"]:
        raise ValueError("试验授权已经过期；未读取密钥")
    names = {"doubao": "豆包 / 火山方舟", "glm": "智谱官网", "deepseek": "DeepSeek 官网", "qwen": "百炼通用 API", "openai": "OpenAI 官网"}
    name = "智谱 / 火山方舟" if config.get("glm_platform") == "volcengine_ark" else names[config["provider"]]
    nonce = uuid.uuid4().hex

    def record(phase):
        write_record(status_directory / f"{config['pilot_id']}-{nonce}-{phase}.json",
                     {"phase": phase, "at_ms": int(time.time() * 1000), "plan_sha256": plan["plan_sha256"], "secret_recorded": False})

    root = tk.Tk()
    root.title(f"AI 共创室 · {name} · 本机密钥输入")
    root.geometry("720x430")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=f"粘贴 {name} API Key", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
    ttk.Label(frame, text="支持 Ctrl+V。输入后显示圆点，请只粘贴一次。\n密钥只用于当前进程，不写入文件或聊天。", font=("Microsoft YaHei UI", 11)).pack(anchor="w", pady=(12, 14))
    value = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=value, show="\u2022", font=("Segoe UI", 13))
    entry.pack(fill="x")
    status = tk.StringVar(value="等待输入")
    ttk.Label(frame, textvariable=status, font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(6, 12))
    ttk.Label(frame, text=f"模型：{config['model']}\n最多 {max_calls} 次请求；每次输出上限 {config['max_output_tokens']} Token；失败立即停止，不重试。\n消费计划 ≤ {config['spend_plan_limit']} {config['rate_card']['currency']}（不是供应商账单硬上限）", font=("Microsoft YaHei UI", 10)).pack(anchor="w")
    actions = ttk.Frame(frame)
    actions.pack(fill="x", pady=(20, 0))
    chosen = []

    def cancel():
        value.set("")
        try:
            record("cancelled")
        finally:
            root.destroy()

    def submit(*_):
        candidate = value.get().strip()
        if not acceptable_password(candidate):
            status.set("输入为空或含空格、控制字符，请全选后重新粘贴。")
            return
        if time.time() * 1000 >= config["expires_at_ms"]:
            cancel()
            return
        try:
            record("submitted")
        except OSError:
            status.set("无法保存输入状态，未提交；请取消后检查本机目录。")
            return
        chosen.append(candidate)
        value.set("")
        root.destroy()

    ttk.Button(actions, text="取消，不调用", command=cancel).pack(side="left")
    button = ttk.Button(actions, text=f"提交密钥并执行已批准的 {max_calls} 次请求", command=submit, state="disabled")
    button.pack(side="right")
    trace_id = value.trace_add("write", lambda *_: button.configure(state="normal" if value.get() else "disabled"))
    root.protocol("WM_DELETE_WINDOW", cancel)
    entry.bind("<Return>", submit)
    root.after(200, lambda: (root.lift(), root.focus_force(), entry.focus_set()))
    root.after(max(1, config["expires_at_ms"] - int(time.time() * 1000)), cancel)
    root.update_idletasks()
    root.geometry(f"{max(720, root.winfo_reqwidth())}x{max(430, root.winfo_reqheight())}")
    try:
        record("opened")
        root.mainloop()
    finally:
        try:
            value.trace_remove("write", trace_id)
            value.set("")
        except tk.TclError:
            pass
        try:
            root.destroy()
        except tk.TclError:
            pass
    if not chosen:
        raise KeyboardInterrupt
    return chosen.pop()
