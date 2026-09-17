"""Provider names, defined once.

A provider name is an implicit contract between the runner that creates a task,
the registry that stores its binding, the adapter that reads it back, and the
adapter manifest that declares it.  Spelling it inline in each module is how
those four drift apart: every file looks correct on its own, and the mismatch
only shows up at dispatch time.  Import from here instead.
"""

# The visible Codex control plane, matching adapters/manifests/codex.yaml.
CODEX_PROVIDER = "codex-app-visible"

# The visible Claude Code control plane, matching adapters/manifests/claude-code.yaml.
CLAUDE_CODE_PROVIDER = "claude-code-visible"
