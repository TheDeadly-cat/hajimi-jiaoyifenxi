"""MockTk only: no window, clipboard, credentials or network access."""
import importlib.util
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("comparison_credentials_tested", BASE / "credentials.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
old = BASE.parent / "news_api_sample_v2/tests/test_credentials.py"
mock_spec = importlib.util.spec_from_file_location("comparison_mocktk_base", old)
m = importlib.util.module_from_spec(mock_spec)
mock_spec.loader.exec_module(m)


class Window(m.Window):
    def lift(self): self.lifted = True


class Widget(m.Widget):
    def focus_force(self): self.focused = True


class MockTk(m.MockTk):
    def Tk(self):
        self.window = Window(self)
        return self.window
    def StringVar(self, master, value=""):
        return super().StringVar(master, value)
    def Entry(self, _, **kwargs):
        entry = Widget(self, **kwargs)
        self.entries.append(entry)
        return entry


class CredentialsTests(unittest.TestCase):
    def invoke(self, scenario):
        tk = MockTk(scenario)
        with patch.dict(sys.modules, {"tkinter": tk}):
            return c.ask_keys(time.time() + 30)

    def test_two_masked_fields_paste_and_clear(self):
        def scenario(tk):
            self.assertTrue(tk.window.lifted)
            self.assertTrue(tk.entries[0].focused)
            for entry in tk.entries:
                self.assertEqual(entry.options["show"], "●")
                entry.bindings["<Control-v>"](None)
                self.assertEqual(entry.bindings["<Control-c>"](None), "break")
                self.assertIn("<Button-3>", entry.bindings)
            for label in tk.labels:
                self.assertNotIn(tk.paste_text, str(label.options.get("text", "")))
                variable = label.options.get("textvariable")
                if variable: self.assertNotIn(tk.paste_text, variable.get())
            tk.buttons[0].options["command"]()
            self.assertTrue(tk.window.closed)
            self.assertEqual(tk.variables[0].get(), "")
            self.assertEqual(tk.variables[1].get(), "")
        self.assertEqual(self.invoke(scenario), {"doubao": "SYNTHETIC_KEY_ONLY", "deepseek": "SYNTHETIC_KEY_ONLY"})

    def test_invalid_input_and_close_prevent_later_submit(self):
        def scenario(tk):
            tk.variables[0].set("\x16SYNTHETIC")
            tk.variables[1].set("SYNTHETIC_KEY_ONLY")
            tk.buttons[0].options["command"]()
            self.assertFalse(tk.window.closed)
            tk.window.protocols["WM_DELETE_WINDOW"]()
            tk.window.bindings["<Return>"](None)
            self.assertEqual(tk.variables[0].get(), "")
            self.assertEqual(tk.variables[1].get(), "")
        with self.assertRaises(c.PromptCancelled): self.invoke(scenario)

    def test_expired_and_invalid_key_formats(self):
        for value in ("", " ", "x\r\n", "x\x7f", "非ASCII", "x" * 4097):
            self.assertFalse(c.valid_key(value))
        tk = MockTk(lambda _: self.fail("should_not_open"))
        with patch.dict(sys.modules, {"tkinter": tk}), self.assertRaises(c.PromptCancelled):
            c.ask_keys(time.time() - 1)
        self.assertFalse(hasattr(tk, "window"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
