"""Write inline secrets to mode 0600 files on the data volume.

``config.json`` stores the file path. The stdio child reads that path and
does not receive the secret in its environment map. An operator-supplied
``*_FILE`` is left untouched. This module never prints secret values.
"""

from __future__ import annotations

import os
import pwd
from collections.abc import Mapping
from pathlib import Path

# Keep these paths in sync with scripts/lib/config.py.
SECRET_DIRECTORY = Path("/var/lib/id42-work/secrets")
DATABASE_FILE_NAME = "database.url"
ODOO_FILE_NAME = "odoo.key"
GITHUB_FILE_NAME = "github.token"
_MAX_BYTES = 8192


def default_database_file() -> str:
    return str(SECRET_DIRECTORY / DATABASE_FILE_NAME)


def default_odoo_file() -> str:
    return str(SECRET_DIRECTORY / ODOO_FILE_NAME)


def default_github_file() -> str:
    return str(SECRET_DIRECTORY / GITHUB_FILE_NAME)


def _one_line(value: str, label: str) -> str:
    text = value.strip()
    if not text or "\n" in text or "\r" in text or "\x00" in text:
        raise SystemExit(f"id42-agent: {label} must be a single line")
    if len(text.encode("utf-8")) > _MAX_BYTES:
        raise SystemExit(f"id42-agent: {label} is too long")
    return text


def _nanobot_owner() -> tuple[int, int] | None:
    if os.geteuid() != 0:
        return None
    try:
        user = pwd.getpwnam("nanobot")
    except KeyError:
        raise SystemExit("id42-agent: user nanobot is missing") from None
    return user.pw_uid, user.pw_gid


def write_secret_file(path: Path, value: str) -> None:
    """Create or replace ``path`` as a regular mode 0600 file.

    A symlink is refused. Root chowns the file to ``nanobot``.
    """
    if path.is_symlink():
        raise SystemExit(f"id42-agent: {path.name} is a symlink")
    directory = path.parent
    if directory.is_symlink():
        raise SystemExit("id42-agent: secrets directory is a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SystemExit(f"id42-agent: cannot write {path.name}: {exc.strerror}") from None
    try:
        os.write(descriptor, (value + "\n").encode("utf-8"))
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    owner = _nanobot_owner()
    if owner is not None:
        uid, gid = owner
        os.chown(directory, uid, gid)
        os.chown(path, uid, gid)


def files_to_write(env: Mapping[str, str], directory: Path) -> list[tuple[Path, str, str]]:
    """Return ``(path, contents, label)`` for inline secrets that have no file yet."""
    planned: list[tuple[Path, str, str]] = []
    url = env.get("ID42_WORK_DATABASE_URL", "")
    if url.strip() and not env.get("ID42_WORK_DATABASE_URL_FILE", "").strip():
        planned.append(
            (
                directory / DATABASE_FILE_NAME,
                _one_line(url, "ID42_WORK_DATABASE_URL"),
                "ID42_WORK_DATABASE_URL",
            )
        )
    odoo = env.get("ODOO_API_KEY", "")
    if odoo.strip() and not env.get("ODOO_API_KEY_FILE", "").strip():
        planned.append(
            (directory / ODOO_FILE_NAME, _one_line(odoo, "ODOO_API_KEY"), "ODOO_API_KEY")
        )
    github = env.get("GITHUB_TOKEN", "")
    if github.strip() and not env.get("ID42_WORK_GITHUB_TOKEN_FILE", "").strip():
        planned.append(
            (
                directory / GITHUB_FILE_NAME,
                _one_line(github, "GITHUB_TOKEN"),
                "GITHUB_TOKEN",
            )
        )
    return planned


def materialize(env: Mapping[str, str] | None = None, *, directory: Path | None = None) -> list[Path]:
    """Write inline secrets under ``directory``. Return the paths written."""
    source = os.environ if env is None else env
    root = SECRET_DIRECTORY if directory is None else directory
    written: list[Path] = []
    for path, contents, _label in files_to_write(source, root):
        write_secret_file(path, contents)
        written.append(path)
    return written


def _become_nanobot() -> None:
    """Switch to uid 1000 before creating secret files.

    The container keeps only ``CHOWN``, ``SETGID``, and ``SETUID``. After the
    entrypoint makes ``/var/lib/id42-work`` mode ``0700`` and owned by
    ``nanobot``, root cannot search that directory. The files have to be
    created as uid 1000.
    """
    if os.geteuid() != 0:
        return
    try:
        user = pwd.getpwnam("nanobot")
    except KeyError:
        raise SystemExit("id42-agent: user nanobot is missing") from None
    try:
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    except OSError as exc:
        raise SystemExit(f"id42-agent: cannot switch to nanobot: {exc.strerror}") from None


def main() -> None:
    _become_nanobot()
    materialize()


if __name__ == "__main__":
    main()
