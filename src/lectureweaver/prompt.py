"""User prompts for login: terminal, or a native dialog on each OS.

The dialog variant exists so an AI harness can start a login while the
password / MFA code is typed by the user into a system dialog and never
passes through the model's context or the harness transcript.

Dialog backends, in order of preference:
* macOS   - AppleScript (``osascript``)
* Windows - PowerShell + WinForms (masked input for secrets)
* Linux   - ``zenity`` (GNOME) or ``kdialog`` (KDE), when a display exists
* any     - Tk (``tkinter``), when a display exists
Prompt texts are handed over in environment variables, so Chinese text and
quotes never need shell escaping.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys


class PromptUnavailable(RuntimeError):
    pass


class Prompter:
    def ask(self, message: str, secret: bool = False, default: str = "") -> str:
        raise NotImplementedError

    def choose(self, message: str, options: list[str]) -> int:
        raise NotImplementedError

    def info(self, message: str) -> None:
        print(message, file=sys.stderr)


class TerminalPrompter(Prompter):
    def ask(self, message: str, secret: bool = False, default: str = "") -> str:
        if secret:
            return getpass.getpass(f"{message}: ")
        suffix = f" [{default}]" if default else ""
        v = input(f"{message}{suffix}: ").strip()
        return v or default

    def choose(self, message: str, options: list[str]) -> int:
        print(message, file=sys.stderr)
        for i, o in enumerate(options, 1):
            print(f"  {i}. {o}", file=sys.stderr)
        while True:
            v = input("选择序号: ").strip()
            if v.isdigit() and 1 <= int(v) <= len(options):
                return int(v) - 1


def _run(cmd: list[str], env_extra: dict[str, str]) -> str:
    env = {**os.environ, **env_extra}
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise KeyboardInterrupt("用户取消了对话框")
    return r.stdout.rstrip("\r\n")


# ------------------------------------------------------------------ macOS
class MacDialogPrompter(Prompter):
    def ask(self, message: str, secret: bool = False, default: str = "") -> str:
        hidden = " with hidden answer" if secret else ""
        script = (
            "display dialog (system attribute \"LW_MSG\") default answer (system attribute \"LW_DEF\")"
            f"{hidden} with title \"LectureWeaver\" buttons {{\"取消\", \"确定\"}} default button \"确定\"\n"
            "text returned of result"
        )
        return _run(["osascript", "-e", script], {"LW_MSG": message, "LW_DEF": default})

    def choose(self, message: str, options: list[str]) -> int:
        script = (
            "set opts to paragraphs of (system attribute \"LW_OPTS\")\n"
            "set r to choose from list opts with title \"LectureWeaver\" with prompt (system attribute \"LW_MSG\") default items {item 1 of opts}\n"
            "if r is false then error number -128\n"
            "item 1 of r"
        )
        v = _run(["osascript", "-e", script], {"LW_MSG": message, "LW_OPTS": "\n".join(options)})
        return options.index(v)

    def info(self, message: str) -> None:
        print(message, file=sys.stderr)
        subprocess.run(["osascript", "-e", "display notification (system attribute \"LW_MSG\") with title \"LectureWeaver\""],
                       capture_output=True, env={**os.environ, "LW_MSG": message})


# ------------------------------------------------------------------ Windows
_PS_ASK = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object Windows.Forms.Form
$f.Text = 'LectureWeaver'; $f.Width = 460; $f.Height = 180; $f.StartPosition = 'CenterScreen'; $f.TopMost = $true
$l = New-Object Windows.Forms.Label; $l.Text = $env:LW_MSG; $l.Left = 12; $l.Top = 12; $l.Width = 420; $l.Height = 36
$t = New-Object Windows.Forms.TextBox; $t.Left = 12; $t.Top = 52; $t.Width = 420; $t.Text = $env:LW_DEF
if ($env:LW_SECRET -eq '1') { $t.UseSystemPasswordChar = $true }
$ok = New-Object Windows.Forms.Button; $ok.Text = '确定'; $ok.Left = 272; $ok.Top = 90; $ok.DialogResult = 'OK'
$no = New-Object Windows.Forms.Button; $no.Text = '取消'; $no.Left = 356; $no.Top = 90; $no.DialogResult = 'Cancel'
$f.Controls.AddRange(@($l, $t, $ok, $no)); $f.AcceptButton = $ok; $f.CancelButton = $no
if ($f.ShowDialog() -ne 'OK') { exit 1 }
[Console]::Out.Write($t.Text)
"""

_PS_CHOOSE = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object Windows.Forms.Form
$f.Text = 'LectureWeaver'; $f.Width = 460; $f.Height = 260; $f.StartPosition = 'CenterScreen'; $f.TopMost = $true
$l = New-Object Windows.Forms.Label; $l.Text = $env:LW_MSG; $l.Left = 12; $l.Top = 12; $l.Width = 420; $l.Height = 36
$b = New-Object Windows.Forms.ListBox; $b.Left = 12; $b.Top = 52; $b.Width = 420; $b.Height = 110
$env:LW_OPTS -split "`n" | ForEach-Object { [void]$b.Items.Add($_) }; $b.SelectedIndex = 0
$ok = New-Object Windows.Forms.Button; $ok.Text = '确定'; $ok.Left = 272; $ok.Top = 172; $ok.DialogResult = 'OK'
$no = New-Object Windows.Forms.Button; $no.Text = '取消'; $no.Left = 356; $no.Top = 172; $no.DialogResult = 'Cancel'
$f.Controls.AddRange(@($l, $b, $ok, $no)); $f.AcceptButton = $ok; $f.CancelButton = $no
if ($f.ShowDialog() -ne 'OK') { exit 1 }
[Console]::Out.Write($b.SelectedIndex)
"""


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


class WindowsDialogPrompter(Prompter):
    def _ps(self, script: str, env: dict[str, str]) -> str:
        return _run([_powershell(), "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script], env)

    def ask(self, message: str, secret: bool = False, default: str = "") -> str:
        return self._ps(_PS_ASK, {"LW_MSG": message, "LW_DEF": default, "LW_SECRET": "1" if secret else "0"})

    def choose(self, message: str, options: list[str]) -> int:
        return int(self._ps(_PS_CHOOSE, {"LW_MSG": message, "LW_OPTS": "\n".join(options)}))


# ------------------------------------------------------------------ Linux
class ZenityPrompter(Prompter):
    def ask(self, message: str, secret: bool = False, default: str = "") -> str:
        if secret:
            return _run(["zenity", "--password", "--title=LectureWeaver"], {})
        return _run(["zenity", "--entry", "--title=LectureWeaver", f"--text={message}", f"--entry-text={default}"], {})

    def choose(self, message: str, options: list[str]) -> int:
        v = _run(["zenity", "--list", "--title=LectureWeaver", f"--text={message}", "--column=选项", *options], {})
        return options.index(v)


class KDialogPrompter(Prompter):
    def ask(self, message: str, secret: bool = False, default: str = "") -> str:
        if secret:
            return _run(["kdialog", "--title", "LectureWeaver", "--password", message], {})
        return _run(["kdialog", "--title", "LectureWeaver", "--inputbox", message, default], {})

    def choose(self, message: str, options: list[str]) -> int:
        args = []
        for i, o in enumerate(options):
            args += [str(i), o]
        return int(_run(["kdialog", "--title", "LectureWeaver", "--menu", message, *args], {}))


# ------------------------------------------------------------------ Tk (anywhere)
class TkPrompter(Prompter):
    def ask(self, message: str, secret: bool = False, default: str = "") -> str:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        v = simpledialog.askstring("LectureWeaver", message, initialvalue=default, show="*" if secret else None, parent=root)
        root.destroy()
        if v is None:
            raise KeyboardInterrupt("用户取消了对话框")
        return v

    def choose(self, message: str, options: list[str]) -> int:
        import tkinter as tk

        root = tk.Tk()
        root.title("LectureWeaver")
        root.attributes("-topmost", True)
        tk.Label(root, text=message, wraplength=420, justify="left").pack(padx=12, pady=8)
        box = tk.Listbox(root, height=min(len(options), 8), width=48)
        for o in options:
            box.insert("end", o)
        box.selection_set(0)
        box.pack(padx=12)
        result: list[int] = []

        def ok():
            sel = box.curselection()
            result.append(sel[0] if sel else 0)
            root.destroy()

        tk.Button(root, text="确定", command=ok).pack(pady=8)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.mainloop()
        if not result:
            raise KeyboardInterrupt("用户取消了选择")
        return result[0]


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _tk_ok() -> bool:
    import importlib.util

    if importlib.util.find_spec("tkinter") is None:
        return False
    return sys.platform in ("darwin", "win32") or _has_display()


def dialog_prompter() -> Prompter:
    if sys.platform == "darwin" and shutil.which("osascript"):
        return MacDialogPrompter()
    if os.name == "nt" and _powershell():
        return WindowsDialogPrompter()
    if sys.platform.startswith("linux") and _has_display():
        if shutil.which("zenity"):
            return ZenityPrompter()
        if shutil.which("kdialog"):
            return KDialogPrompter()
    if _tk_ok():
        return TkPrompter()
    raise PromptUnavailable(
        "这台电脑没有可用的图形对话框（例如无桌面的 Linux）。请在终端里直接运行 `lw auth login` 输入密码；"
        "不要把密码发到聊天里。"
    )


def default_prompter(gui: bool | None = None) -> Prompter:
    if gui is None:
        gui = not sys.stdin.isatty()
    return dialog_prompter() if gui else TerminalPrompter()


# Backwards-compatible name used by earlier code.
DialogPrompter = MacDialogPrompter
