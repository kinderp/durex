# Session Approval Deduplication

This document records the fixes made to session id extraction and the PTY
approval flow after duplicated output interpretation was observed.

## Problem

The queue runner extracts a Codex `session_id` from command output so interrupted
tasks can resume. The previous extraction returned the first matching id. If an
output log contained multiple runs or retry fragments, Durex could persist an old
session id and resume the wrong session.

The PTY runner also reads terminal output incrementally and keeps a rolling
buffer so the approval detector can inspect recent text. The previous request id
was built from the extracted command plus the whole display context.

That was unstable because the context changes as more terminal output arrives.
The same approval prompt could therefore produce multiple ids, causing Durex to
handle the same approval more than once.

The detector also treated plain mentions of `approve` or `approval` as approval
prompts. This could re-trigger detection after a process continued and printed a
normal status line containing those words.

## Fixes

- `approval_detector.make_request_id()` now fingerprints only the extracted
  command and the stable prompt line, not the full rolling context.
- `codex_queue.extract_session_id()` now scans all candidate ids and returns the
  latest occurrence in the output.
- `approval_detector.looks_like_approval_prompt()` now requires an interactive
  prompt signal such as `[y/N]`, `(y/n)`, `yes/no`, or approval wording with a
  question mark.
- `pty_runner.run_pty_command()` clears the rolling detection buffer after an
  approval decision is written back to the PTY. This prevents stale prompt text
  from being re-read as a new request.

## Expected Behavior

The persisted Codex `session_id` should be the last valid session id shown in
the output.

One visible approval prompt should produce one `ApprovalAuditEvent`.

Additional output printed after the approval decision should not create another
event unless the terminal shows a new approval prompt.

## Regression Coverage

- `tests/test_approval_detector.py` verifies that unrelated context growth does
  not change a request id.
- `tests/test_approval_detector.py` verifies that ordinary text containing
  `approve` is not treated as a prompt.
- `tests/test_codex_queue.py` verifies that session extraction returns the
  latest candidate from multi-session output.
- `tests/test_pty_runner.py` runs a real PTY subprocess and verifies that one
  prompt is handled exactly once.

## Local Verification

The repository can be checked without network access:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile approval_detector.py approval_policy.py pty_runner.py telegram_bridge.py codex_queue.py
```

When `pytest` is installed, run:

```bash
python3 -m pytest -q
```
