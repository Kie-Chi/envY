"""Build a native macOS bundle without changing the shared Electron package."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil


def build_bundle(source: Path, destination: Path, metadata: dict, icon: Path) -> None:
    shutil.copytree(source, destination, symlinks=True)
    for directory, directories, files in os.walk(destination):
        for path in [Path(directory), *[Path(directory) / name for name in directories + files]]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o200)
    contents = destination / "Contents"
    (contents / "MacOS/Electron").rename(contents / "MacOS/T3 Code")
    info_file = contents / "Info.plist"
    info = plistlib.loads(info_file.read_bytes())
    info.update(metadata)
    info.update(CFBundleName="T3 Code", CFBundleDisplayName="T3 Code",
                CFBundleExecutable="T3 Code Launcher", CFBundleIconFile="t3code.icns")
    info_file.write_bytes(plistlib.dumps(info))
    shutil.copyfile(icon, contents / "Resources/t3code.icns")
    for helper in list((contents / "Frameworks").glob("Electron Helper*.app")):
        old_name = helper.stem
        new_name = old_name.replace("Electron", "T3 Code", 1)
        native = helper / "Contents/MacOS" / old_name
        native.rename(native.with_name(new_name))
        helper_info = helper / "Contents/Info.plist"
        data = plistlib.loads(helper_info.read_bytes())
        suffix = new_name.removeprefix("T3 Code Helper").strip(" ()").lower()
        data.update(CFBundleName=new_name, CFBundleDisplayName=new_name,
                    CFBundleExecutable=new_name,
                    CFBundleIdentifier="com.t3tools.t3code.helper" + ("." + suffix if suffix else ""))
        helper_info.write_bytes(plistlib.dumps(data))
        helper.rename(helper.with_name(new_name + ".app"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("icon", type=Path)
    args = parser.parse_args()
    build_bundle(args.source, args.destination, json.loads(args.metadata.read_text()), args.icon)
