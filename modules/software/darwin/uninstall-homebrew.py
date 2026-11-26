"""Reconcile only explicitly named Homebrew removals; never clean unrelated packages."""

import json
import os
from pathlib import Path
import subprocess
import sys


def normalize(name):
    for prefix in ("homebrew/core/", "homebrew/cask/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def selected(name, targets):
    name = normalize(name)
    return any(
        name == normalize(target)
        or ("/" not in normalize(target) and name.rsplit("/", 1)[-1] == target)
        for target in targets
    )


def reconcile(brew, declaration):
    # Override inherited settings that could widen an explicit uninstall.
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_AUTOREMOVE="1")
    env.pop("HOMEBREW_CASK_OPTS", None)
    removals = []
    # Read every relevant inventory before making any changes. A broken inventory
    # is an error, rather than evidence that a package is already absent.
    for kind, key in (("--cask", "casks"), ("--formula", "formulae")):
        targets = declaration[key]
        if not targets:
            continue
        result = subprocess.run(
            [brew, "list", kind, "--full-name", "-1"],
            env=env, text=True, stdout=subprocess.PIPE, check=True,
        )
        installed = list(dict.fromkeys(result.stdout.splitlines()))
        names = [name for name in installed if selected(name, targets)]
        if names:
            removals.append((kind, names))
    for kind, names in removals:
        print("Homebrew explicit uninstall: " + " ".join(names), flush=True)
        # Batch formulae so dependencies among the explicitly removed formulae
        # do not require --ignore-dependencies. Remove casks first.
        subprocess.run([brew, "uninstall", kind, "--", *names], env=env, check=True)


def main():
    try:
        declaration = json.loads(Path(sys.argv[2]).read_text())
        reconcile(sys.argv[1], declaration)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: Homebrew explicit uninstall failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
