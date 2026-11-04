"""Darwin-only machine and secret fields."""

from envy.schemas.common.config import FieldDef
from envy.schemas.validators import is_url


MACHINE_FIELDS = [
    FieldDef(group="DARWIN_SERVICES", dest="machine",
             path="envy.darwin.services.mihomo.mode",
             prompt="Mihomo service mode", default_fn=lambda: "none",
             choices=["none", "manual", "keep"], required=True),
    FieldDef(group="DARWIN_SERVICES", dest="machine",
             path="envy.darwin.services.mihomo.tun",
             prompt="Mihomo TUN mode", default_fn=lambda: "false",
             choices=["true", "false"],
             condition=lambda values: values.get("envy.darwin.services.mihomo.mode") != "none",
             nix_type="bool"),
    FieldDef(group="DARWIN_SERVICES", dest="machine",
             path="envy.darwin.services.openssh.mode",
             prompt="OpenSSH service mode", default_fn=lambda: "manual",
             choices=["none", "manual", "keep"], required=True),
]

SECRET_FIELDS = [
    FieldDef(group="DARWIN_SECRET", dest="secret", yaml_path="proxy/url", path="proxy_url",
             prompt="Proxy URL", default_fn=lambda: "",
             condition=lambda values: values.get("envy.darwin.services.mihomo.mode") != "none",
             validators=[is_url]),
]

OBSOLETE_MACHINE_KEYS = ["envy.darwin.proxy.mode", "envy.darwin.proxy.tun"]
OBSOLETE_SECRET_PATHS: list[str] = []
