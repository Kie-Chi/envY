"""Metadata refresh in a private cache, without root or package database changes."""
import os
import tempfile
from pathlib import Path

from envy.process import run_process
from envy.sources.model import SourceError
from envy.sources.native.apt import effective_options


def refresh(source):
    if source.context.get("manager") != "apt":
        raise SourceError("private metadata refresh is currently supported by APT", "unsupported")
    from envy.sources.adapters import apt_context
    from dataclasses import replace
    if not source.parameters.get("system"):
        with apt_context(replace(source, registered=False)):
            return
    with tempfile.TemporaryDirectory(prefix="envy-apt-refresh-") as temporary:
        lists = Path(temporary) / "lists"
        lists.mkdir()
        (lists / "partial").mkdir()
        hooks = Path(temporary) / "private.conf"
        hooks.write_text("#clear APT::Update::Post-Invoke;\n#clear APT::Update::Post-Invoke-Success;\n")
        with effective_options() as options:
            result = run_process(["apt-get", "-c", str(hooks), *options, "-o", f"Dir::State::lists={lists}",
                                  "-o", f"Dir::Cache={temporary}", "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache=",
                                  "-o", "Debug::NoLocking=1", "-o", "APT::Update::Error-Mode=any",
                                  "-o", "APT::Update::Post-Invoke::=", "-o", "APT::Update::Post-Invoke-Success::=",
                                  "update"], capture=True, check=False, timeout=120,
                                 env={**os.environ, "LC_ALL": "C"})
        if result.returncode:
            raise SourceError("signed metadata synchronization failed", "source-unavailable")
