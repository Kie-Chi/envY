"""Interactive confirmation, selection menu, and USB key discovery for envy key."""

import os
import sys
from pathlib import Path
from typing import List, Optional

from rich.prompt import Confirm

from envy import log


def prompt_input(message: str) -> str:
    """Read one interactive value without background output moving the cursor."""
    # prompt_toolkit costs ~0.1 s to import; only interactive key flows need it.
    from prompt_toolkit import prompt as pt_prompt

    with log.interactive_prompt():
        return pt_prompt(message)


# --yes/-y global flag
_yes_flag = False


def _set_yes_flag(yes: bool):
    global _yes_flag
    _yes_flag = yes


def confirm(prompt_text: str) -> bool:
    """Require explicit y/n confirmation. Auto-answer yes with --yes/-y."""
    if _yes_flag:
        log.hint(f"{prompt_text} → auto-yes")
        return True
    return Confirm.ask(prompt_text, console=log.console)


# ==========================================
# INTERACTIVE MENU (prompt_toolkit)
# ==========================================


def _select_menu(items: list, title: str = "Select") -> Optional[tuple]:
    """Arrow-key navigation menu using prompt_toolkit Application.
    items: list of (fragments, action_type, data)
    Returns (action_type, data) on Enter, None on Esc/q."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import FormattedTextControl, Layout, Window
    from prompt_toolkit.styles import Style as PtStyle

    class MenuState:
        cursor = 0
        result = None

    state = MenuState()

    def render():
        lines = [("class:title", f"  {title}\n")]
        for i, (fragments, action, data) in enumerate(items):
            base = "class:cursor" if i == state.cursor else "class:normal"
            prefix = "  ► " if i == state.cursor else "    "
            lines.append((base, prefix))
            for style_suffix, text in fragments:
                if style_suffix:
                    lines.append((f"class:{style_suffix}", text))
                else:
                    lines.append((base, text))
            lines.append((base, "\n"))
        lines.append(("class:bottom", "\n  Enter: confirm  │  Esc/q: cancel  │  ↑↓: navigate"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        if state.cursor > 0:
            state.cursor -= 1
        event.app.invalidate()

    @kb.add("down")
    def _(event):
        if state.cursor < len(items) - 1:
            state.cursor += 1
        event.app.invalidate()

    @kb.add("enter")
    def _(event):
        _, action, data = items[state.cursor]
        state.result = (action, data)
        event.app.exit()

    @kb.add("escape")
    @kb.add("q")
    def _(event):
        state.result = None
        event.app.exit()

    content = FormattedTextControl(render)
    layout = Layout(Window(content=content))
    style = PtStyle.from_dict({
        "title": "#ansicyan bold",
        "cursor": "bg:#ansicyan #ansiblack bold",
        "normal": "",
        "usb": "#ansiyellow bold",
        "bottom": "#ansigray",
    })

    app_instance = Application(layout=layout, key_bindings=kb, style=style, full_screen=True)
    app_instance.run()
    return state.result


# ==========================================
# USB KEY SCANNING
# ==========================================


def scan_usb_key_files() -> List[Path]:
    """Scan mounted USB drives for .age/.age.txt files."""
    candidates = []
    mount_dirs = []

    if sys.platform == "darwin":
        mount_dirs = [Path("/Volumes")]
    else:
        mount_dirs = [Path(f"/media/{os.getenv('USER', '')}"), Path("/mnt")]

    for mount_dir in mount_dirs:
        if not mount_dir.exists():
            continue
        for volume in mount_dir.iterdir():
            if not volume.is_dir():
                continue

            recovery_dir = volume / "recovery"
            if recovery_dir.exists() and recovery_dir.is_dir():
                for f in sorted(recovery_dir.iterdir()):
                    if f.is_file() and (f.name.endswith(".age") or f.name.endswith(".age.txt")):
                        candidates.append(f)

            for f in sorted(volume.iterdir()):
                if f.is_file() and (f.name.endswith(".age") or f.name.endswith(".age.txt")):
                    candidates.append(f)

    return candidates
