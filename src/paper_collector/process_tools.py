"""Isolated subprocesses with bounded lifetime, including their descendants."""

from __future__ import annotations

import os
import signal
import subprocess


def run_isolated(command, *, timeout, env=None, input=None):
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=os.name == "posix",
    ) as process:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except BaseException as error:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            stdout, stderr = process.communicate()
            if isinstance(error, subprocess.TimeoutExpired):
                error.stdout, error.stderr = stdout, stderr
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
