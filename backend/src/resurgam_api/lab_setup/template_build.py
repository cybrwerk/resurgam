"""Moved to `resurgam_worker.lab_build.probe_template` (ADR 0027) so the lab
agent can run the same build locally. This module *is* that module: every
existing import and test patch keeps working."""

import sys

from resurgam_worker.lab_build import probe_template as _impl

sys.modules[__name__] = _impl
