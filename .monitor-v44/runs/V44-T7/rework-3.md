Rework-3: Added independent rollback_state API and evidence path. Rollback restores legacy artifacts into `.vibe/rollback/`, records restored hashes and `current_namespace_preserved`, and never overwrites current namespace or source state. Added upgrade→migration→rollback regression test asserting source SHA-256 unchanged and rollback evidence.
Validation:
- python3 -m unittest tests.test_installation tests.test_v44_install_compatibility -v: 13 passed
- python3 -m py_compile vibe_guide/installation.py vibe_guide/cli.py: exit 0
- Controlled upgrade fixture: source state workflow_version=2/schema_version=1; migrate_state created v44-4.2.2 namespace and migration evidence; rollback_state created separate rollback namespace, source hash unchanged, current namespace preserved; exit 0.
- Packaging upgrade/rollback evidence uses the controlled legacy namespace fixture because no separately published old repository artifact is available. This is not inferred from force-reinstall.
