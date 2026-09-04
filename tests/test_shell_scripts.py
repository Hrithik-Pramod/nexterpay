"""Structural checks on the shell scripts.

These are the scripts nobody runs until something has gone wrong - taking a
backup, restoring one, bootstrapping a server. The Python is covered by 277
tests; the shell was covered by nothing, and on 4 September that cost a
deploy: `zcat file | grep -q pattern` under `set -o pipefail` reported failure
on a dump that was perfectly good.

Nothing here executes the scripts. They talk to Docker and to Postgres, so
running them in CI is not on offer. What is on offer is refusing to ship the
two mistakes that have actually been made in them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parents[1] / "scripts").glob("*.sh"))


def test_there_are_scripts_to_check() -> None:
    """Guards that find nothing are worse than no guards, because they
    report green. If the scripts move, this suite must be told."""
    assert SCRIPTS, "no shell scripts found - has the folder moved?"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_it_parses(script: Path) -> None:
    """A syntax error in restore.sh is discovered at the worst possible
    moment. `bash -n` costs nothing."""
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"{script.name} does not parse:\n{result.stderr}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_short_circuiting_reader_on_the_left_of_a_pipe(script: Path) -> None:
    """The SIGPIPE trap, in the general form.

    `producer | grep -q` (or `head`, or `grep -m1`) stops reading as soon as
    it has its answer. That closes the pipe under a producer which is still
    writing, so the producer dies of SIGPIPE and exits 141 - and `pipefail`,
    which every one of these scripts sets and should set, hands that 141 to
    the caller as the status of the whole pipeline.

    The result is a check that passes on small inputs and fails on large
    ones. It is not flaky, which would at least be noticeable; it flips once,
    permanently, when the data crosses the pipe buffer. That is precisely
    when the data has become worth protecting.

    Use `grep -c`, which must read to the end, and compare the count.
    """
    text = script.read_text()
    if "pipefail" not in text:
        pytest.skip(f"{script.name} does not set pipefail")

    offenders = [
        (n, line.strip())
        for n, line in enumerate(text.splitlines(), 1)
        if re.search(r"\|\s*(z?grep\s+(-\w*q|\S*\s+-\w*q)|head\b|grep\s+-\w*m\s*\d)", line)
        and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"{script.name} pipes into a command that stops reading early, "
        f"under pipefail:\n"
        + "\n".join(f"  line {n}: {line}" for n, line in offenders)
        + "\nSee looks_like_a_nexterpay_dump in scripts/lib-dump.sh."
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_it_stops_on_error(script: Path) -> None:
    """A backup script that carries on past a failed command writes a file
    that looks like a backup and is not one."""
    if script.name == "lib-dump.sh":
        pytest.skip("sourced, not executed - it inherits the caller's settings")
    assert "set -euo pipefail" in script.read_text(), (
        f"{script.name} does not set -euo pipefail"
    )


def test_the_dump_check_reads_to_the_end() -> None:
    """The specific fix, tested by running it - against a payload larger
    than the pipe buffer, which is the only size at which the old version
    was wrong.

    A test of the same shape as the code would pass on both versions. This
    one fails on the version that shipped.
    """
    lib = Path(__file__).resolve().parents[1] / "scripts" / "lib-dump.sh"
    script = f"""
    set -euo pipefail
    . {lib}
    tmp=$(mktemp -d)
    {{ echo "CREATE TABLE public.work_items ("
       for i in $(seq 1 40000); do echo "COPY public.events line $i padding padding"; done
    }} | gzip > "$tmp/big.sql.gz"
    {{ for i in $(seq 1 40000); do echo "COPY public.other line $i padding padding"; done
    }} | gzip > "$tmp/wrong.sql.gz"

    looks_like_a_nexterpay_dump "$tmp/big.sql.gz"   || {{ echo REJECTED_A_GOOD_DUMP; exit 1; }}
    looks_like_a_nexterpay_dump "$tmp/wrong.sql.gz" && {{ echo ACCEPTED_A_BAD_DUMP; exit 1; }}
    rm -rf "$tmp"
    echo OK
    """
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "OK" in result.stdout, (
        f"dump verification is wrong:\n{result.stdout}\n{result.stderr}"
    )


def test_backup_and_restore_agree_on_what_a_dump_looks_like() -> None:
    """They must use the same test, or restore refuses files backup wrote.

    Before the fix they held the same string in two places, which is how one
    could be corrected and the other left - and the one left wrong would be
    restore.sh, discovered during a recovery.
    """
    for name in ("backup.sh", "restore.sh"):
        text = (Path(__file__).resolve().parents[1] / "scripts" / name).read_text()
        assert "looks_like_a_nexterpay_dump" in text, (
            f"{name} does not use the shared check"
        )
        assert "lib-dump.sh" in text, f"{name} does not source the shared check"
