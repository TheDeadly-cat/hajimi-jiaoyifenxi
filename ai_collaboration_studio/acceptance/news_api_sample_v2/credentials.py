"""Masked, user-operated input. Importing this module does not open a window."""
import time

DEMO_TEXT = "SYNTHETIC_DEMO_KEY"


class PromptCancelled(Exception):
    pass


def valid_key(value):
    # Match the unchanged NewsAnalysis.configure contract; do not silently trim.
    return type(value) is str and 1 <= len(value) <= 4096 and all(32 < ord(char) < 127 for char in value)


def ask_api_key(*, expires_at, tk_module=None, synthetic_only=False):
    if tk_module is None:
        import tkinter as tk_module
    if expires_at <= time.time():
        raise PromptCancelled()
    tk = tk_module
    window = tk.Tk()
    window.title("输入框演示 · 仅合成文字" if synthetic_only else "一次性审核 · 输入 API key")
    window.resizable(False, False)
    value = tk.StringVar(master=window, value="")
    feedback = tk.StringVar(master=window, value="尚未输入；支持 Ctrl+V 或右键粘贴。")
    state = {"closed": False, "key": None}

    def cancel(*_):
        if state["closed"]:
            return "break"
        state["closed"] = True
        state["key"] = None
        value.set("")
        window.destroy()
        return "break"

    def submit(*_):
        if state["closed"]:
            return "break"
        text = value.get()
        if not valid_key(text):
            feedback.set("输入无效：需要 1–4096 个 ASCII 可见字符，不能含空格、换行或控制字符。")
            return "break"
        if synthetic_only and text != DEMO_TEXT:
            feedback.set("演示只接受 SYNTHETIC_DEMO_KEY；不要输入真实密钥。")
            return "break"
        if time.time() >= expires_at:
            return cancel()
        state["key"] = text
        state["closed"] = True
        value.set("")
        window.destroy()
        return "break"

    def changed(*_):
        if not state["closed"]:
            feedback.set("已输入 " + str(len(value.get())) + " 个字符；内容始终隐藏。")

    try:
        intro = ("仅粘贴 SYNTHETIC_DEMO_KEY；不要输入真实密钥。\n这是输入框演示，不加载执行器、不调用 API、不消耗授权。"
                 if synthetic_only else "仅本次已批准的一次请求；密钥不保存。\n粘贴后下方字符数会变化，不能看到明文。")
        tk.Label(window, text=intro, padx=20, pady=12).pack()
        entry = tk.Entry(window, textvariable=value, show="●", width=62, exportselection=False)
        entry.pack(padx=20, pady=4)
        tk.Label(window, textvariable=feedback, wraplength=490, padx=20, pady=8).pack()
        tk.Button(window, text="确认合成输入（不调用 API）" if synthetic_only else "确认输入，继续已批准的一次请求", command=submit).pack(padx=20, pady=4)
        tk.Button(window, text="取消，不发送请求", command=cancel).pack(padx=20, pady=8)
        value.trace_add("write", changed)
        menu = tk.Menu(window, tearoff=False)

        def paste(*_):
            if not state["closed"]:
                entry.focus_set()
                entry.event_generate("<<Paste>>")
            return "break"

        menu.add_command(label="粘贴", command=paste)

        def popup(event):
            if not state["closed"]:
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()
            return "break"

        entry.bind("<Control-v>", paste)
        entry.bind("<Control-V>", paste)
        entry.bind("<Button-3>", popup)
        # Keep this input from exporting an unmasked key back to the clipboard.
        entry.bind("<Control-c>", lambda _: "break")
        entry.bind("<Control-x>", lambda _: "break")
        entry.bind("<Control-C>", lambda _: "break")
        entry.bind("<Control-X>", lambda _: "break")
        window.bind("<Return>", submit)
        window.bind("<Escape>", cancel)
        window.protocol("WM_DELETE_WINDOW", cancel)
        window.after(max(1, int((expires_at - time.time()) * 1000)), cancel)
        entry.focus_set()
        window.mainloop()
        if not state["closed"]:
            cancel()
        result = state["key"]
        state["key"] = None
        if result is None:
            raise PromptCancelled()
        return result
    finally:
        state["key"] = None
        if not state["closed"]:
            cancel()
