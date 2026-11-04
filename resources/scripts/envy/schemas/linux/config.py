"""Linux-only machine fields."""

from envy.schemas.common.config import FieldDef


MACHINE_FIELDS = [
    FieldDef(group="LINUX", dest="machine", path="envy.linux.desktop",
             prompt="Linux desktop", default_fn=lambda: "gnome",
             choices=["gnome", "niri", "all", "none"], required=True),
    FieldDef(group="LINUX", dest="machine", path="envy.linux.option",
             prompt="Linux machine type", default_fn=lambda: "desktop",
             choices=["desktop", "server"], required=True),
]

SECRET_FIELDS: list[FieldDef] = []
OBSOLETE_MACHINE_KEYS: list[str] = []
OBSOLETE_SECRET_PATHS: list[str] = []
