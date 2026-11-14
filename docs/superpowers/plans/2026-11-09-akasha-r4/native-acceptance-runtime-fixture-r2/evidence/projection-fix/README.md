# Projection fix evidence

Scope: integration reviewer r2 rework in the existing writer worktree `v528-r4-preconditions`.

## Changes

- `tests/test_strict_first_owner_cli.py`: tamper fixtures now use the protected `create-payloads` record for prompt and worker binding changes; public mailbox projections are no longer treated as executable payloads.
- `vibe_guide/adapters/task_provider.py`: controlled-create replay validates the protected payload executable digest and uses the protected request for anchor lineage; immutable public action identity, request bytes, lock, and anchor checks remain enforced.
- `vibe_guide/cli.py`: initial recovery classifies foreign requests using the effective protected create payload, so a request from the same plan with a foreign run is fail-closed as `blocked_unknown`.

## Verification

- Targeted command:
  `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest -q tests.test_strict_first_owner_cli tests.test_audit_create_claim`
- Result: `Ran 72 tests ... OK`, exit 0.
- `python3 -m vibe_guide --help`: exit 0.
- `python3 -m py_compile vibe_guide/adapters/task_provider.py vibe_guide/cli.py tests/test_strict_first_owner_cli.py`: exit 0.
- `git diff --check`: exit 0.
- Full `python3 -m unittest discover -s tests -v`: not completed; the process remained in a long-running existing test after about 2m43s and was stopped. Full-suite result remains `UNKNOWN`.

## Boundary

No AC evidence, run state, tasks, registry, lease, native receipt, commit, push, PR, merge, tag, Release, or deploy was changed by this evidence update. Six real ACs and Release readiness remain unvalidated.

`developer.status=DEVELOPER_PROJECTION_FIX_COMPLETE`
