"""Run a command with Landlock filesystem write confinement.

The production host cannot create user namespaces, so bubblewrap is not
available there.  Landlock needs no namespace and is available on the 6.11
kernel used by app-server.

Reads and execution stay broadly allowed.  Writes (create, remove, rename,
truncate, chmod through open, etc.) are allowed only below the directories
passed with ``--workspace`` plus a small set of character devices.  The
launcher fails closed when Landlock cannot be applied.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import struct
import sys
from pathlib import Path

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_CREATE_RULESET_VERSION = 1
PR_SET_NO_NEW_PRIVS = 38

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13
FS_TRUNCATE = 1 << 14

READ_RIGHTS = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
WRITE_RIGHTS = (
    FS_WRITE_FILE
    | FS_REMOVE_DIR
    | FS_REMOVE_FILE
    | FS_MAKE_CHAR
    | FS_MAKE_DIR
    | FS_MAKE_REG
    | FS_MAKE_SOCK
    | FS_MAKE_FIFO
    | FS_MAKE_BLOCK
    | FS_MAKE_SYM
)
# These are needed by the model's tooling on the production host.
DEVICE_WRITES = ("/dev/null", "/dev/zero", "/dev/tty")


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _syscall(libc: ctypes.CDLL, number: int, *args) -> int:
    result = libc.syscall(number, *args)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def landlock_abi() -> int:
    """Return the kernel's Landlock ABI, or 0 when unavailable."""
    if sys.platform != "linux":
        return 0
    try:
        result = _libc().syscall(
            SYS_LANDLOCK_CREATE_RULESET, ctypes.c_void_p(0), 0,
            LANDLOCK_CREATE_RULESET_VERSION,
        )
    except AttributeError:
        return 0
    return max(result, 0)


def handled_rights(abi: int) -> int:
    rights = READ_RIGHTS | WRITE_RIGHTS
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    return rights


def writable_rights(abi: int) -> int:
    rights = WRITE_RIGHTS
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    return rights


def _create_ruleset(libc: ctypes.CDLL, abi: int) -> int:
    rights = handled_rights(abi)
    # handled_access_net is only part of the struct from ABI 4 onwards.
    payload = struct.pack("=QQ", rights, 0) if abi >= 4 else struct.pack("=Q", rights)
    buffer = ctypes.create_string_buffer(payload, len(payload))
    return _syscall(
        libc, SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(buffer), len(payload), 0
    )


def _add_path_rule(
    libc: ctypes.CDLL, ruleset: int, path: Path, allowed: int
) -> None:
    try:
        descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        raise OSError(exc.errno, f"cannot open Landlock path {path}: {exc.strerror}") from exc
    try:
        # struct landlock_path_beneath_attr is packed: u64 + s32.
        payload = struct.pack("=Qi", allowed, descriptor)
        buffer = ctypes.create_string_buffer(payload, len(payload))
        _syscall(
            libc,
            SYS_LANDLOCK_ADD_RULE,
            ruleset,
            LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(buffer),
            0,
        )
    finally:
        os.close(descriptor)


def apply_landlock(workspaces: list[Path]) -> int:
    if sys.platform != "linux":
        raise RuntimeError("Landlock is only available on Linux")
    abi = landlock_abi()
    if abi < 1:
        raise RuntimeError("Landlock is not available in this kernel")
    libc = _libc()
    ruleset = _create_ruleset(libc, abi)
    try:
        # Read and execute everything; DAC still protects root-only files
        # because the agent runs as an unprivileged user.
        _add_path_rule(libc, ruleset, Path("/"), READ_RIGHTS)
        write = writable_rights(abi)
        for workspace in workspaces:
            if not workspace.is_absolute():
                raise RuntimeError(f"workspace must be absolute: {workspace}")
            _add_path_rule(libc, ruleset, workspace, write)
        for device in DEVICE_WRITES:
            path = Path(device)
            if path.exists():
                _add_path_rule(libc, ruleset, path, FS_WRITE_FILE)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(error)}")
        _syscall(libc, SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0)
    finally:
        os.close(ruleset)
    return abi


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", action="append", default=[], required=False)
    parser.add_argument("--probe", action="store_true", help="print the Landlock ABI and exit")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.probe:
        print(landlock_abi())
        return 0
    if not args.workspace:
        parser.error("at least one --workspace is required")
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    try:
        apply_landlock([Path(item).resolve() for item in args.workspace])
    except (OSError, RuntimeError) as exc:
        print(f"sandbox_exec: {exc}", file=sys.stderr)
        return 126
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"sandbox_exec: cannot exec {command[0]}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
