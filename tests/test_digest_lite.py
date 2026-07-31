"""Item (7): digest_lite baseline — sample jsonl -> non-empty digest,
--since-line behaves.

DESIGN.md §3.5 step 2 only describes digest_lite.py's role (compress only the lines after the
cursor) without freezing an exact CLI surface. This test assumes the
simplest reading consistent with that description and with `scripts/` being
plain-Python entry points invoked positionally:

    python3 scripts/digest_lite.py <transcript.jsonl> [--since-line N]

writing the digest to stdout. If the real interface differs, this file is
the one to adjust — the two properties under test (non-empty digest; a
--since-line cutoff strictly shrinks the output) are the load-bearing
contract, not the exact flag spelling.
"""
import subprocess
import sys

from conftest import SCRIPTS_DIR

DIGEST_SCRIPT = SCRIPTS_DIR / "digest_lite.py"


def _run_digest(args, timeout=30):
    return subprocess.run(
        [sys.executable, str(DIGEST_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_digest_lite_produces_nonempty_digest(fake_transcript_path):
    proc = _run_digest([str(fake_transcript_path)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != ""


def test_digest_lite_since_line_shrinks_output(fake_transcript_path):
    full = _run_digest([str(fake_transcript_path)])
    assert full.returncode == 0, full.stderr

    total_lines = len(
        fake_transcript_path.read_text(encoding="utf-8").splitlines()
    )

    since_all = _run_digest([str(fake_transcript_path), "--since-line", str(total_lines)])
    assert since_all.returncode == 0, since_all.stderr
    assert len(since_all.stdout) < len(full.stdout), (
        "digesting only the lines after the last one should yield strictly "
        "less content than digesting from the start"
    )
