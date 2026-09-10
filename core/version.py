"""Single source of truth for the application version.

Reported by ``GET /health``, ``GET /`` and the OpenAPI schema. Keep it in step
with the newest released section of ``CHANGELOG.md``: a release commit moves the
``Unreleased`` block to ``## [x.y.z]`` and bumps this constant in the same
commit, so ``/health`` always names the release the build descends from.

This module exists because the version previously had no home: it was hardcoded
as ``"1.0.0"`` in four places across the API layer while the project was in fact
releasing 0.4.x, so ``/health`` misreported the running release entirely.
"""

__version__ = "0.4.8"
