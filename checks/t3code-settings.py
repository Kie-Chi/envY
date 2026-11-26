"""Check the actual rendered settings for both platform compositions."""

import json
import shlex
from pathlib import Path
import sys

fixtures = json.loads(Path(sys.argv[1]).read_text())
for fixture in fixtures:
    files = fixture["files"]
    assert set(files) == {"t3code-server", "t3code-client", "t3code-theme", "t3code-desktop"}, fixture["name"]
    desktop = json.loads(Path(files["t3code-desktop"]).read_text())
    assert desktop == {"localEnvironmentEnabled": False}
    assert fixture["serviceUnitCommand"] == fixture["serviceCommand"]
    expected_name = "com.t3tools.t3code.service" if fixture["platform"] == "darwin" else "t3code.service"
    assert fixture["serviceUnitName"] == expected_name
    environment = fixture["serviceEnvironment"]
    if isinstance(environment, list):
        environment = dict(entry.split("=", 1) for entry in environment)
    assert environment["T3CODE_HOME"] == fixture["serviceHome"]
    assert environment["T3CODE_NIX_PACKAGE"] == fixture["servicePackage"]
    assert environment["T3CODE_NIX_VERSION"]
    assert environment["T3_BOOT_SERVICE_UNIT"] == expected_name + (".plist" if fixture["platform"] == "darwin" else "")
    runner = Path(fixture["serviceCommand"]).read_text()
    merge = next(shlex.split(line) for line in runner.splitlines() if " merge " in line)
    declared = json.loads(Path(merge[-1]).read_text())
    settings = fixture["serviceSettings"]
    assert declared == {"T3CODE_HOST": settings["host"], "T3CODE_PORT": str(settings["port"]),
                        "T3CODE_LAN": str(settings["lan"]).lower()}
    assert "serve" not in merge
    assert " run " in runner and "/bin/t3" in runner
    server = json.loads(Path(files["t3code-server"]).read_text())
    client = json.loads(Path(files["t3code-client"]).read_text())
    theme = json.loads(Path(files["t3code-theme"]).read_text())
    providers = server["providerInstances"]
    assert set(providers) == set(fixture["providers"]), fixture["name"]
    for key, command in (("pi", "pi"), ("claudeAgent", "claude")):
        if key in providers:
            assert providers[key]["config"]["binaryPath"] == fixture["profileBin"] + "/" + command
    if "codex" in providers:
        assert providers["codex"]["config"]["binaryPath"] == fixture["codexBinary"]
        selection = server["defaultModelSelection"]
        assert selection == server["textGenerationModelSelection"]
        assert selection["instanceId"] == "codex"
        assert selection["model"] in providers["codex"]["config"]["customModels"]
    assert server["defaultTheme"] == "envy-tokyo-night"
    assert theme["appearance"] == server["defaultAppearance"]
    assert client["fontFamilyCode"] == client["fontFamilyTerminal"]
    assert isinstance(client["fontSizeCode"], (int, float)) and client["fontSizeCode"] > 0
    assert client["fontSizeCode"] == client["fontSizeTerminal"]
print(f"T3 Code settings verified for {len(fixtures)} machines")
