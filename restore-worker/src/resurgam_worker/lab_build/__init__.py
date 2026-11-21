"""Builds of the lab node's VM templates (docs/adr/0027): the Recovery Lab
probe appliance template and the Disk Mounts share helper template.

Shared by both ways RESURGAM runs them: the backend over a one-time SSH
session (`resurgam_api.lab_setup`), and `resurgam-lab-agent` locally on its
node from a structured job spec (`lab_agent.setup_jobs`) -- the agent never
executes commands sent by the backend, only these builds with validated
parameters. No backend imports here: the agent's package does not ship the
backend.
"""
