"""Appliance updates via signed release bundles (docs/adr/0024).

``bundle`` is the pure verification core shared by the host updater
(``updater``) and the API's upload pre-check. Nothing in this package may
import FastAPI or the database layer -- the host updater runs it outside the
web app, as root, from the native workers' venv.
"""
