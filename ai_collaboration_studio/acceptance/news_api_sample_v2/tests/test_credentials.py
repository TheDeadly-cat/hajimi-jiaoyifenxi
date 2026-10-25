"""MockTk only. No real window, clipboard or credential is accessed."""
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from acceptance.news_api_sample_v2.credentials import DEMO_TEXT, PromptCancelled, ask_api_key, valid_key


class Variable:
    def __init__(self, value):
        self.value, self.callbacks = value, []

    def set(self, value):
        self.value = value
        for callback in self.callbacks:
            callback()

    def get(self):
        return self.value

    def trace_add(self, _, callback):
        self.callbacks.append(callback)


class Widget:
    def __init__(self, module, **kwargs):
        self.module, self.options, self.bindings = module, kwargs, {}

    def pack(self, **_):
        pass

    def bind(self, event, callback):
        self.bindings[event] = callback

    def focus_set(self):
        pass

    def event_generate(self, event):
        assert event == "<<Paste>>"
        self.options["textvariable"].set(self.module.paste_text)

    def add_command(self, **kwargs):
        self.options.update(kwargs)

    def tk_popup(self, *_):
        pass

    def grab_release(self):
        pass


class Window(Widget):
    def __init__(self, module):
        super().__init__(module)
        self.protocols, self.closed, self.timers = {}, False, []

    def title(self, _):
        pass

    def resizable(self, *_):
        pass

    def protocol(self, event, callback):
        self.protocols[event] = callback

    def after(self, interval, callback):
        self.timers.append((interval, callback))

    def destroy(self):
        self.closed = True

    def mainloop(self):
        self.module.scenario(self.module)


class MockTk:
    def __init__(self, scenario):
        self.scenario = scenario
        self.variables, self.entries, self.buttons, self.labels = [], [], [], []
        self.paste_text = "SYNTHETIC_KEY_ONLY"

    def Tk(self):
        self.window = Window(self)
        return self.window

    def StringVar(self, master, value):
        var = Variable(value)
        self.variables.append(var)
        return var

    def Entry(self, _, **kwargs):
        entry = Widget(self, **kwargs)
        self.entries.append(entry)
        return entry

    def Label(self, _, **kwargs):
        label = Widget(self, **kwargs)
        self.labels.append(label)
        return label

    def Button(self, _, **kwargs):
        button = Widget(self, **kwargs)
        self.buttons.append(button)
        return button

    def Menu(self, _, **kwargs):
        self.menu = Widget(self, **kwargs)
        return self.menu


class CredentialTests(unittest.TestCase):
    def test_ascii_validation_rejects_control_v_whitespace_and_non_ascii(self):
        for invalid in ("", " ", "key with spaces", "key\r\n", "\x16key", "key\x00", "key\x7f", "密钥", "x" * 4097):
            self.assertFalse(valid_key(invalid))
        self.assertTrue(valid_key("SYNTHETIC_VISIBLE_ASCII-key_123"))

    def test_paste_updates_count_but_never_displays_key(self):
        def scenario(tk):
            entry = tk.entries[0]
            self.assertEqual(entry.options["show"], "●")
            self.assertEqual(entry.bindings["<Control-v>"](None), "break")
            self.assertIn(str(len(tk.paste_text)), tk.variables[1].get())
            self.assertNotIn(tk.paste_text, tk.variables[1].get())
            self.assertEqual(entry.bindings["<Control-c>"](None), "break")
            tk.buttons[0].options["command"]()
            self.assertEqual(tk.variables[0].get(), "")
        tk = MockTk(scenario)
        self.assertEqual(ask_api_key(expires_at=time.time() + 10, tk_module=tk), tk.paste_text)
        self.assertTrue(tk.window.closed)

    def test_close_cancels_and_later_enter_cannot_continue(self):
        def scenario(tk):
            tk.variables[0].set(tk.paste_text)
            tk.window.protocols["WM_DELETE_WINDOW"]()
            tk.window.bindings["<Return>"](None)
            tk.buttons[0].options["command"]()
            self.assertEqual(tk.variables[0].get(), "")
        tk = MockTk(scenario)
        with self.assertRaises(PromptCancelled):
            ask_api_key(expires_at=time.time() + 10, tk_module=tk)

    def test_invalid_input_does_not_submit_and_cancel_is_explicit(self):
        def scenario(tk):
            tk.variables[0].set("\x16SYNTHETIC")
            tk.buttons[0].options["command"]()
            self.assertFalse(tk.window.closed)
            self.assertIn("输入无效", tk.variables[1].get())
            tk.buttons[1].options["command"]()
        with self.assertRaises(PromptCancelled):
            ask_api_key(expires_at=time.time() + 10, tk_module=MockTk(scenario))

    def test_expired_prompt_never_constructs_window(self):
        tk = MockTk(lambda _: self.fail("no_window_expected"))
        with self.assertRaises(PromptCancelled):
            ask_api_key(expires_at=time.time() - 1, tk_module=tk)
        self.assertFalse(hasattr(tk, "window"))

    def test_right_click_paste_and_synthetic_only_input(self):
        def scenario(tk):
            tk.variables[0].set("OTHER_VISIBLE_ASCII")
            tk.buttons[0].options["command"]()
            self.assertFalse(tk.window.closed)
            self.assertIn("演示只接受", tk.variables[1].get())
            self.assertIn("<Button-3>", tk.entries[0].bindings)
            tk.paste_text = DEMO_TEXT
            tk.menu.options["command"]()
            self.assertIn(str(len(DEMO_TEXT)), tk.variables[1].get())
            tk.buttons[0].options["command"]()
        self.assertEqual(ask_api_key(expires_at=time.time() + 10, tk_module=MockTk(scenario), synthetic_only=True), DEMO_TEXT)


if __name__ == "__main__":
    unittest.main()
