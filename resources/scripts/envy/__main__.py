from envy import log
from envy.main import cli
from envy.process import CommandError, render_command_error
from envy.utils import DEVICE_METADATA_HINT, DeviceMetadataError

try:
    cli(prog_name="envy")
except CommandError as exc:
    render_command_error(exc)
    raise SystemExit(exc.returncode) from None
except DeviceMetadataError as exc:
    log.error("device", str(exc))
    log.hint(DEVICE_METADATA_HINT)
    raise SystemExit(1) from None
