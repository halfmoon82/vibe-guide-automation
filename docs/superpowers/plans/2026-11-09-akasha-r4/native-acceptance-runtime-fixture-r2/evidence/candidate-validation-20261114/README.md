# V5.2.8 candidate validation — 2026-11-14

Full test commands were run independently with Python 3.11 and Python 3.9. Each ran 5297 tests and exited 1. The 3.11 result was 132 failures and 29 errors (3 skipped, 4 expected failures); the 3.9 result also exited 1 with broad cross-module failures and additional Python 3.9/native-root temporary-subprocess errors.

The failures are retained as UNKNOWN. They are not attributed to the projection-fix because the affected areas span adoption, handoff, self-report, native-root, WorkBuddy and legacy mailbox fixtures, while the projection-fix targeted suite remained 72/72 green. No speculative production or test changes were made.

Isolated staging build succeeded: wheel and sdist both report version 5.2.8; staged wheel help exited 0; source help exited 0; `git diff --check` exited 0.

No real AC evidence, run state, registry, lease, native receipt, commit, push, PR/MR, merge, tag, Release or deploy was changed.

`developer.status=DEVELOPER_CANDIDATE_VALIDATION_COMPLETE`
