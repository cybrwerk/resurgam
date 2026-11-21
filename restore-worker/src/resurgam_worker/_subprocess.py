from __future__ import annotations

import subprocess
from collections.abc import Callable


def run_subprocess(
    command: list[str],
    *,
    timeout: float,
    on_timeout: Callable[[list[str], int, str], Exception],
    env: dict[str, str] | None = None,
    capture_text: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Runs `command` with a fixed timeout, raising `on_timeout(command,
    -1, str(exc))`'s exception on a hang instead of letting
    `subprocess.TimeoutExpired` propagate. Shared by every client wrapper
    in this package (`pbs_client._subprocess.run`, `pve_client.qm._run`)
    -- each supplies its own domain error type and a timeout tuned to
    its own binaries, rather than reimplementing this same
    run-with-timeout-and-wrap shape per module.

    `input_text` is written to the child's stdin (e.g. a secret for
    `qm guest exec --pass-stdin`, which must never appear in argv).
    """
    try:
        return subprocess.run(  # noqa: S603 - fixed binary + argument list, no shell
            command, capture_output=True, text=capture_text, timeout=timeout, check=False, env=env, input=input_text
        )
    except subprocess.TimeoutExpired as exc:
        raise on_timeout(command, -1, str(exc)) from exc
