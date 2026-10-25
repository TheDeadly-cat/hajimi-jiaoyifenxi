"""User-operated masked credentials and safe progress; never read stored keys."""
import threading
import time


class PromptCancelled(Exception):
    pass


def valid_key(value):
    return type(value) is str and 1 <= len(value) <= 4096 and all(32 < ord(c) < 127 for c in value)


def ask_keys(expires_at):
    import tkinter as tk
    if time.time() >= expires_at:
        raise PromptCancelled()
    window = tk.Tk()
    window.title("5 条摘录对照 · 豆包与 DeepSeek Flash")
    window.resizable(False, False)
    state = {"keys": None, "closed": False}
    values = {name: tk.StringVar(master=window) for name in ("doubao", "deepseek")}
    tk.Label(window, text="已批准：最多 10 次请求，1 元应用预约预算；无重试。\n密钥仅本次进程使用，不保存。两项都输入后才开始。\n45 秒为网络超时；预约预算不是供应商账单硬上限。", padx=20, pady=12).pack()

    def close():
        if not state["closed"]:
            state["closed"] = True
            for value in values.values():
                value.set("")
            window.destroy()

    def cancel(*_):
        state["keys"] = None
        close()
        return "break"

    feedback = tk.StringVar(master=window, value="支持 Ctrl+V 或右键粘贴；不要在旧终端里粘贴。")
    first_entry = None
    for name, label in (("doubao", "豆包 / 火山方舟 API key"), ("deepseek", "DeepSeek 官方 API key")):
        tk.Label(window, text=label).pack(anchor="w", padx=20)
        entry = tk.Entry(window, textvariable=values[name], show="●", width=65, exportselection=False)
        if first_entry is None:
            first_entry = entry
        entry.pack(padx=20, pady=4)
        count = tk.StringVar(master=window, value="已输入 0 个字符")
        values[name].trace_add("write", lambda *_, n=name, c=count: c.set("已输入 " + str(len(values[n].get())) + " 个字符"))
        tk.Label(window, textvariable=count).pack(anchor="w", padx=20)
        menu = tk.Menu(window, tearoff=False)
        def paste(*_, e=entry):
            e.focus_set()
            e.event_generate("<<Paste>>")
            return "break"
        menu.add_command(label="粘贴", command=paste)
        def popup(event, m=menu):
            try:
                m.tk_popup(event.x_root, event.y_root)
            finally:
                m.grab_release()
            return "break"
        entry.bind("<Control-v>", paste)
        entry.bind("<Control-V>", paste)
        entry.bind("<Button-3>", popup)
        for event in ("<Control-c>", "<Control-C>", "<Control-x>", "<Control-X>"):
            entry.bind(event, lambda _: "break")
    tk.Label(window, textvariable=feedback, padx=20, pady=8).pack()
    def submit(*_):
        if state["closed"]:
            return "break"
        if time.time() >= expires_at:
            return cancel()
        result = {name: value.get() for name, value in values.items()}
        if not all(valid_key(value) for value in result.values()):
            feedback.set("两项都须为 1–4096 个可见 ASCII 字符，不能含空格或换行。")
            result.clear()
            return "break"
        state["keys"] = result
        close()
        return "break"
    tk.Button(window, text="确认输入，开始已批准的对照", command=submit).pack(pady=4)
    tk.Button(window, text="取消，不发送请求", command=cancel).pack(pady=10)
    window.protocol("WM_DELETE_WINDOW", cancel)
    window.bind("<Escape>", cancel)
    window.bind("<Return>", submit)
    window.after(max(1, int((expires_at - time.time()) * 1000)), cancel)
    window.lift()
    first_entry.focus_force()
    window.mainloop()
    result = state["keys"]
    state["keys"] = None
    if result is None:
        raise PromptCancelled()
    return result


def run_with_progress(work):
    """Network work runs on one worker; close only cancels subsequent requests."""
    import queue
    import tkinter as tk
    window = tk.Tk()
    window.title("小样本对照执行中")
    messages = queue.Queue()
    cancel = threading.Event()
    result = {}
    label = tk.StringVar(master=window, value="正在准备第一条请求……")
    tk.Label(window, textvariable=label, padx=24, pady=24, wraplength=500).pack()
    def stop():
        cancel.set()
        label.set("已取消后续请求；正在进行的单次请求只等待结束，不重试。")
    tk.Button(window, text="取消剩余请求", command=stop).pack(pady=12)
    window.protocol("WM_DELETE_WINDOW", stop)
    def worker():
        try:
            result["value"] = work(cancel, messages.put)
        except BaseException:
            result["failed"] = True
        finally:
            messages.put(None)
    thread = threading.Thread(target=worker, daemon=False)
    thread.start()
    def poll():
        while True:
            try:
                message = messages.get_nowait()
            except queue.Empty:
                break
            if message is None:
                window.destroy()
                return
            label.set(message)
        window.after(100, poll)
    window.after(100, poll)
    window.mainloop()
    thread.join()
    if result.get("failed"):
        raise RuntimeError("comparison_worker_unconfirmed")
    return result["value"]
