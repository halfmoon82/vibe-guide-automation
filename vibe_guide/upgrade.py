"""Explicit, local project upgrade path.

Upgrading only reconciles the versioned local project scaffold.  It never
invokes a provider, starts a run, or touches deployment state.
"""

from .initializer import InitResult, init_project


def upgrade_project(paths, confirm: bool) -> InitResult:
    """Apply the current scaffold migration after explicit confirmation."""
    if not confirm:
        return InitResult(False, [])
    return init_project(paths, True)
