# 0027 — Lab node pairing with a one-time code, and template builds run by the agent

Status: Accepted (2026-10-11)

## Context

Setting up a lab node needed a root (or sudo) password or private key typed into RESURGAM's web UI: once
for the agent install and again for every template build (probe appliance, Disk Mounts share helper).
RESURGAM never stored those credentials, but they still passed through the browser and the appliance, and
the appliance had to reach the node over SSH. The user asked for something "click-admin compatible",
possibly with one-time credentials. Two options were compared: a throwaway SSH key per setup job, and
pairing the agent once and letting it do the rest. The user chose pairing.

## Decision

### Pairing: one command, one-time code
"Pair node" in the Lab Compute wizard creates a `lab_node_pairings` row and shows a command, plus a link
to Proxmox's own web shell for that node (`/?console=shell&xtermjs=1&node=…`):

```
curl -fsSk -o /root/resurgam-pair.sh '<api>/lab-setup/bootstrap/p/<pairing id>/script' \
  && echo '<sha256>  /root/resurgam-pair.sh' | sha256sum -c - && sh /root/resurgam-pair.sh <code>
```

- **The code** has 80 bits (four groups of four characters, no 0/O/1/I/L). It is single-use, expires after
  30 minutes (`LAB_PAIRING_CODE_TTL_MINUTES`), and a new code revokes older unused ones. Only its SHA-256
  is stored.
- **TLS trust bootstrap:** the node cannot verify the appliance's certificate yet, because Caddy's
  `tls internal` CA is unknown to it. So the first download skips TLS verification (`-k`) and the command
  pins the script's SHA-256 instead; a modified script never runs. The command is copied from the
  authenticated browser session, so the pin is trustworthy. The script embeds the appliance CA (Caddy's
  root certificate only, copied by the `ca-export` sidecar into a volume the backend mounts read-only at
  `/appliance-ca` -- never Caddy's private keys) and verifies TLS with it (`--cacert`) for
  everything after: the agent `.deb`, whose SHA-256 the script also carries, and the pair call. Without
  that CA file (a public certificate), the system trust store is used.
- **The code never crosses the unverified download.** Anyone on the path can read that first request, and
  with the code they could call `/pair` first and get the cluster agent token, which can fetch PBS
  encryption keys and share passwords. So the script is fetched by the pairing's id
  (`GET /lab-setup/bootstrap/p/{pairing_id}/script`, served only while the pairing is unused, unexpired and
  not replaced), which is not a secret. The code is the script's argument, and the script sends it only
  over the verified connection: `GET /lab-setup/bootstrap/{code}/agent.deb` and the pair call. There is
  no route that serves the script by code.
- **The script holds no secret.** `POST /lab-setup/bootstrap/{code}/pair` uses up the code and returns the
  RESURGAM part of `/etc/default/resurgam-lab-agent`: backend URL, a new cluster agent token, node name and
  overlay storage. It also sets `pve_clusters.lab_agent_node`. The script then appends the node's PBS
  token, which it asks for on the node itself and which never leaves the node; only "provided yes/no" is
  reported. It also appends `RESURGAM_LAB_AGENT_CA_FILE`, then enables and starts the agent.
- **Safety checks:** creating a code refuses a node the cluster does not have (when PVE answers) and a
  `RECOVERY_LAB_PUBLIC_BACKEND_URL` on a loopback address, which on the node would be the node itself. The
  script refuses to run unless it is root on a PVE node, the host name matches the node the code was made
  for, and the Debian release matches the agent package's. The pair call is not retried (the code works
  once, so a retry after a lost answer would only hear "already used"); a refusal shows the HTTP status
  and RESURGAM's message (404: unknown, used or expired; 409: wrong node).
- **The cluster's display name** appears in comments of the script and of the env file. It may not hold
  control characters, and is reduced to letters, digits, spaces and `._-()` there anyway, so a line break
  cannot start a shell line.
- **One agent per cluster,** as before: the cluster token is replaced at pairing, exactly as the SSH
  install did.
- The agent no longer requires a PBS token to start. Without one it runs template builds and appliance
  snippet jobs, but no job that maps a backup.

### Template builds as agent jobs, never commands
A paired agent builds the probe appliance template and the share helper template on its own node.
`POST /pve-clusters/{id}/lab-setup/agent-jobs` queues a row in `lab_agent_setup_jobs`; its `spec` holds
only structured values: VMID, storages, image source, the uplink NIC settings and address, and the
release. The agent:

- picks the job up (`/lab-agent/setup-jobs`, claim, progress, status);
- validates every field (regexes, integer ranges, IPv4 parsing; `node` must be its own);
- downloads the prober `.deb` with its SHA-256 (`/lab-agent/setup-artifacts`);
- runs the build through a local shell.

**The agent never executes command text it receives.** Otherwise a compromised appliance would get root
on every lab node, and the boundary stays where it was before.

To make that possible, the build code moved from the backend to `resurgam_worker.lab_build`
(`probe_template`, `share_helper_template`, the uplink NIC helpers, `StepFailed`, plus protocols for the
shell and the step log). The backend modules `lab_setup/template_build.py` and
`disk_mounts/share_helper_template.py` now alias the moved modules (`sys.modules`), so the SSH path,
imports and test patches are unchanged. The backend's SSH `RemoteShell` and the agent's `LocalShell`
satisfy the same `Shell` protocol; the backend's `JobLog` and the agent's `ReportingLog` satisfy
`BuildLog`.

**How a build runs and finishes:**
- The agent runs one build at a time, in a thread, so lab runs and disk mounts keep going.
- It sends its step log on every step change and at least every 30 s.
- A queued job that nobody claims within 15 minutes, or a running job silent for 10 minutes, is failed
  when next read.
- On success, the job's `result.template_vmid` must equal the spec's VMID; it then becomes the cluster's
  lab template or share helper template.
- The uplink address is released when the job ends; it is a template-build allocation, so the 2-hour TTL
  covers a lost job.
- Agent jobs and guided SSH jobs exclude each other per cluster: starting either kind answers 409 while a
  job of the other kind is queued or running. A stale-job check re-reads the job under a row lock, so it
  never overwrites a claim the agent just made.
- `GET /pve-clusters/{id}/lab-setup/jobs/{id}` serves both kinds. Agent jobs survive an API restart and
  carry `runner: "agent"`.

### UI
The wizard now puts **Lab agent before Probe appliance**. Its default mode is "Pair node with a one-time
code"; guided SSH and manual remain. Once a node is paired, the template step defaults to "Build with the
paired lab agent (no login)". Disk Mounts setup offers "Build with lab agent" when an agent is paired.

## Consequences

- Admins type no SSH credentials into RESURGAM for the normal path. The appliance needs no SSH access to
  lab nodes, only node → appliance HTTPS, which the agent needs anyway.
- The node needs `curl` and `apt` (present on PVE). The agent package and the node's Debian release must
  still match.
- The SSH routes stay for nodes that cannot be paired (no outbound HTTPS to the appliance), and for
  re-installs.
- The agent package now also ships the build code; both are built from the same tree as the backend
  (`lab-packages` stage), so versions match.
