# Security Policy

RESURGAM holds API tokens for your Proxmox Backup Server and Proxmox VE systems and can read and
restore backup data. We take security reports seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report vulnerabilities privately through GitHub:
[**Report a vulnerability**](https://github.com/cybrwerk/resurgam/security/advisories/new)
(repository → *Security* → *Report a vulnerability*).

Please include:

- the RESURGAM version (shown in the web UI under *Administration → System Settings → Updates*, or
  the `VERSION` file),
- what an attacker needs (network position, role, credentials) and what they can achieve,
- steps to reproduce or a proof of concept,
- PBS / PVE versions, if relevant.

## What to expect

- We acknowledge your report within **5 working days**.
- We keep you updated while we investigate and agree on a disclosure date with you.
- Fixes ship as a signed appliance update; the release notes credit you unless you prefer not.

RESURGAM is a public beta maintained by a small team, so these are best-effort targets, not an SLA.

## Supported versions

During the beta only the **latest release** receives security fixes. Please update before
reporting, and keep your appliance on the latest version.

## Scope

In scope: the RESURGAM backend, web UI, appliance image and update mechanism, the restore worker,
`resurgam-lab-agent` and `resurgam-lab-prober`.

Out of scope: vulnerabilities in Proxmox Backup Server or Proxmox VE themselves (report those to
[Proxmox](https://www.proxmox.com/en/about/security-advisories)), and findings that need an
already-compromised `platform_admin` account or root on the appliance.
