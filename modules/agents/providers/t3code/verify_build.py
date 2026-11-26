"""Reject builds missing Pi widgets or the Nix-managed service integration."""

from pathlib import Path
import sys


def verify_build(server_directory: Path) -> None:
    server_chunks = [
        path.read_text()
        for path in server_directory.rglob("*")
        if path.suffix in {".js", ".mjs", ".cjs"}
        and "client" not in path.relative_to(server_directory).parts
    ]
    for marker in ("goal-tasks", "currentTaskInferred", "queue-messages", "queueMessages"):
        if not any(marker in chunk for chunk in server_chunks):
            raise ValueError(f"Bundled server is missing Pi widget support: {marker}")
    for marker in ("T3CODE_NIX_PACKAGE", "T3CODE_NIX_VERSION"):
        if not any(marker in chunk for chunk in server_chunks):
            raise ValueError(f"Bundled server is missing Nix service support: {marker}")
    client_directory = server_directory / "client"
    client_chunks = [path.read_text() for path in client_directory.rglob("*.js")]
    for marker in ("Goal tasks", "Queued follow-up messages"):
        if not any(marker in chunk for chunk in client_chunks):
            raise ValueError(f"Bundled client is missing Pi widget support: {marker}")

    for marker in ("provider.queue.control", "queue-control-result"):
        if not any(marker in chunk for chunk in server_chunks):
            raise ValueError(f"Bundled server is missing Pi queue controls: {marker}")
    for marker in ("Delivery plan", "Edit queued follow-up"):
        if not any(marker in chunk for chunk in client_chunks):
            raise ValueError(f"Bundled client is missing Pi queue controls: {marker}")


if __name__ == "__main__":
    verify_build(Path(sys.argv[1]))
    print("Goal, Queue, and Nix service support verified in the bundled application")
