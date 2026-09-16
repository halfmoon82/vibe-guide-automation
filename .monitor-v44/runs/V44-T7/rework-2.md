Rework-2: Added real CLI `migrate-state` command (parser choice, dispatch, JSON output with migration/rollback evidence). Added run_cli entrypoint test. Corrected mixed-version logic to compare only same-domain package/config aliases and legacy workflow; normal schema/plan/provider domain diversity is compatible.
Packaging evidence:
- `python3 setup.py bdist_wheel --dist-dir /tmp/v44-t7-dist.VrcL`: exit 0, vibe_guide-4.2.2-py3-none-any.whl
- `python3 setup.py sdist --dist-dir /tmp/v44-t7-dist.VrcL`: exit 0, vibe-guide-4.2.2.tar.gz
- clean venv wheel install exit 0; sdist force-reinstall exit 0; imported version 4.2.2
- clean venv source install exit 0; imported version 4.2.2
Upgrade/rollback runtime path not exercised beyond pip force-reinstall; no claim of full rollback behavior.
Targeted tests: 12 total (T7 4 + installation 8) passed; py_compile and CLI test passed.
