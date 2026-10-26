"""Platform-neutral machine and secret fields."""

import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional

from envy.schemas.validators import (
    is_email,
    is_global_launcher_gesture,
    is_terminal_scratchpad_gesture,
    is_url,
    non_empty,
)
from envy.utils import ENVY_ROOT, HOME_DIR


@dataclass
class FieldDef:
    group: str
    dest: str
    path: str
    yaml_path: str = ""
    prompt: str = ""
    default_fn: Callable[[], str] = field(default_factory=lambda: lambda: "")
    choices: list[str] = field(default_factory=list)
    condition: Optional[Callable[[dict], bool]] = None
    validators: list[Callable[[str], Optional[str]]] = field(default_factory=list)
    error_msg: str = ""
    ignore: bool = False
    required: bool = False
    nix_type: str = "string"


COMMON_MACHINE_FIELDS = [
    FieldDef(group="BASE", dest="machine", path="envy.user.name", prompt="System username",
             default_fn=lambda: os.getenv("USER", "chi"), validators=[non_empty], required=True),
    FieldDef(group="BASE", dest="machine", path="envy.user.home", prompt="Home directory",
             default_fn=lambda: str(HOME_DIR), validators=[non_empty], required=True),
    FieldDef(group="GIT", dest="machine", path="envy.git.name", prompt="Git user name",
             default_fn=lambda: os.getenv("USER", "chi"), validators=[non_empty], required=True),
    FieldDef(group="GIT", dest="machine", path="envy.git.email", prompt="Git user email",
             default_fn=lambda: f"{os.getenv('USER','chi')}@{subprocess.getoutput('hostname -f')}",
             validators=[is_email], required=True),
    FieldDef(group="HABITS", dest="machine", path="envy.habits.terminalScratchpad.gesture",
             prompt="Terminal scratchpad gesture", default_fn=lambda: "F12",
             validators=[is_terminal_scratchpad_gesture], ignore=True, required=True),
    FieldDef(group="HABITS", dest="machine", path="envy.habits.globalLauncher.gesture",
             prompt="Global launcher gesture", default_fn=lambda: "Option+Space",
             validators=[is_global_launcher_gesture], ignore=True, required=True),
    FieldDef(group="ENV", dest="machine", path="envy.repository.path", prompt="envY repository path",
             default_fn=lambda: str(ENVY_ROOT), ignore=True, required=True),
    FieldDef(group="VSCODE", dest="machine", path="envy.vscode.mode", prompt="VS Code config mode",
             default_fn=lambda: "remote", choices=["remote", "local"], required=True),
    FieldDef(group="MIRRORS", dest="machine", path="envy.mirrors.mode", prompt="Network mirror mode",
             default_fn=lambda: os.getenv("ENVY_MIRROR", "china"),
             choices=["upstream", "china"], required=True),
    FieldDef(group="AMUX", dest="machine", path="envy.amux.skipPermissions",
             prompt="Skip agent permission prompts in amux containers", default_fn=lambda: "true",
             choices=["true", "false"], required=True, nix_type="bool"),
]

# These fields were machine-managed before endpoints and agents became
# dynamic. They remain readable only as a migration source; they must not be
# rendered, edited, or written back by the normal config commands.
LEGACY_LLM_MACHINE_FIELDS = [
    FieldDef(group="LLM", dest="machine", path="envy.llm.steps.url", prompt="StepFun API base URL",
             default_fn=lambda: os.getenv("STEPFUN_BASE_URL", ""),
             validators=[non_empty, is_url], required=True),
    FieldDef(group="LLM", dest="machine", path="envy.llm.steps.model", prompt="StepFun default model",
             default_fn=lambda: "step-3.7-flash", validators=[non_empty], required=True),
    FieldDef(group="LLM", dest="machine", path="envy.llm.deepseek.url", prompt="DeepSeek API base URL",
             default_fn=lambda: "https://api.deepseek.com", validators=[non_empty, is_url], required=True),
    FieldDef(group="LLM", dest="machine", path="envy.llm.deepseek.model", prompt="DeepSeek default model",
             default_fn=lambda: "deepseek-v4-pro", validators=[non_empty], required=True),
]

LEGACY_MACHINE_KEYS = [field.path for field in LEGACY_LLM_MACHINE_FIELDS]

COMMON_SECRET_FIELDS = [
    FieldDef(group="SECRET", dest="secret", yaml_path="home/passwd", path="home_passwd",
             prompt="System password", default_fn=lambda: os.getenv("USER", ""),
             validators=[non_empty], required=True),
    FieldDef(group="SECRET", dest="secret", yaml_path="llm/steps/apikey", path="llm_steps_apikey",
             prompt="StepFun API Key",
             default_fn=lambda: os.getenv("STEPFUN_API_KEY", os.getenv("API_KEY", "")),
             validators=[non_empty], required=True),
    FieldDef(group="SECRET", dest="secret", yaml_path="llm/deepseek/apikey", path="llm_deepseek_apikey",
             prompt="DeepSeek API Key", default_fn=lambda: ""),
]

LEGACY_CONFIG_PATHS = {
    "envy.user.name": "home.user",
    "envy.user.home": "home.dir",
    "envy.repository.path": "dotfiles.path",
    "envy.git.name": "git.name",
    "envy.git.email": "git.email",
    "envy.vscode.mode": "vscode.mode",
    "envy.llm.steps.url": "llm.steps.url",
    "envy.llm.steps.model": "llm.steps.model",
    "envy.llm.deepseek.url": "llm.deepseek.url",
    "envy.llm.deepseek.model": "llm.deepseek.model",
}

OBSOLETE_MACHINE_KEYS = ["envy.llm.dashscope.url", "envy.llm.dashscope.model"]
OBSOLETE_SECRET_PATHS = ["llm/dashscope/apikey"]
