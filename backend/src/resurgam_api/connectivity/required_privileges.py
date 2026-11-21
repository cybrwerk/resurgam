"""The ONE source of truth for which privileges RESURGAM's own API tokens
need on a PBS instance and on a PVE cluster.

Used by:
- the connection check (`connectivity.health`) to report missing privileges,
- token provisioning (`connectivity.token_provisioning`) to grant exactly these,
- the API (`GET /pbs-instances/required-privileges`,
  `GET /pve-clusters/required-privileges`) so the UI can show the list and
  the manual setup commands *before* an admin connects anything.

Every privilege/role name below was checked against the vendor's own
primary sources (2026-10-06):
- PVE: pve-docs "User Management" chapter (chapter-pveum.html, section
  "Privileges") and the API viewer schema (pve-docs/api-viewer/apidoc.js)
  for the per-endpoint permission checks quoted in each `reason`.
  Note PVE 9 replaced `VM.Monitor` with the `VM.GuestAgent.*` family; the
  guest-agent proxy endpoints `POST .../agent/ping` and
  `GET .../agent/network-get-interfaces` require `VM.GuestAgent.Audit` (or
  `VM.GuestAgent.Unrestricted`) on `/vms/{vmid}`.
- PBS: pbs docs "User Management" (user-management.html) and the role
  definitions in `pbs-api-types/src/acl.rs` (git.proxmox.com, proxmox.git):
  `Audit` = Sys.Audit + Datastore.Audit; `DatastoreAdmin` = Datastore.Audit/
  Modify/Read/Verify/Backup/Prune; `RemoteAdmin` = Remote.Audit/Modify/Read.
  PBS has no custom roles (the ACL `role` parameter is a fixed enum), so
  these three built-in roles are the narrowest combination that covers
  everything below.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RequiredPrivilege:
    """One privilege RESURGAM's token needs. `path` is the ACL path the
    privilege is checked on (the token is granted everything at `/` with
    propagation, so a grant on `/` satisfies every path below)."""

    name: str
    path: str
    reason: str


PBS_REQUIRED_PRIVILEGES: tuple[RequiredPrivilege, ...] = (
    RequiredPrivilege("Sys.Audit", "/", "Read version, node status and task history (Collector, connection test)."),
    RequiredPrivilege("Datastore.Audit", "/datastore", "List datastores, namespaces, backup groups and snapshots."),
    RequiredPrivilege("Datastore.Read", "/datastore", "Read backup contents for file-level restore and indexing."),
    RequiredPrivilege(
        "Datastore.Modify", "/datastore", "Set maintenance mode and the garbage-collection schedule of a datastore."
    ),
    # Sync-job rules from the PBS api-viewer (POST/PUT /config/sync, POST
    # /admin/sync/{id}/run): a pull sync job needs Datastore.Backup on the
    # target datastore and Remote.Read on the source remote; remove-vanished
    # additionally needs Datastore.Prune (verified 2026-10-06).
    RequiredPrivilege(
        "Datastore.Prune", "/datastore", "Create, edit and run prune jobs; sync jobs with 'remove vanished'."
    ),
    RequiredPrivilege("Datastore.Verify", "/datastore", "Create, edit and run verify jobs."),
    RequiredPrivilege(
        "Datastore.Backup",
        "/datastore",
        "Owner-level access to backup groups (group notes, snapshot ownership) and running sync jobs into a datastore.",
    ),
    RequiredPrivilege("Remote.Audit", "/remote", "List remotes and sync jobs."),
    RequiredPrivilege("Remote.Modify", "/remote", "Create, edit and delete remotes and sync jobs."),
    RequiredPrivilege(
        "Remote.Read", "/remote", "Create, edit and run pull sync jobs (read data from the source remote)."
    ),
)

# Built-in PBS roles granted to a RESURGAM-created token at `/` (see module
# docstring for their exact privilege contents).
PBS_ROLES: tuple[str, ...] = ("Audit", "DatastoreAdmin", "RemoteAdmin")


PVE_REQUIRED_PRIVILEGES: tuple[RequiredPrivilege, ...] = (
    RequiredPrivilege("Sys.Audit", "/", "Read nodes, tasks and vzdump backup jobs (GET /cluster/backup)."),
    RequiredPrivilege("Sys.Modify", "/", "Create, edit, enable/disable vzdump backup jobs (POST/PUT /cluster/backup)."),
    RequiredPrivilege(
        "Datastore.Allocate",
        "/storage",
        "Delete volumes of restored/lab VMs on cleanup; apply a backup job's retention (prune-backups) on Run now.",
    ),
    RequiredPrivilege(
        "Datastore.AllocateSpace",
        "/storage",
        "Allocate disks on the target storage for restores, Recovery Lab clones and Instant Recovery disk moves.",
    ),
    RequiredPrivilege("VM.Allocate", "/vms", "Create restored VMs/containers and remove Recovery Lab VMs afterwards."),
    RequiredPrivilege("VM.Audit", "/vms", "List guests and read VM configuration."),
    RequiredPrivilege(
        "VM.Backup", "/vms", "Restore guests from PBS backups and start backup jobs (Backup Jobs → Run now)."
    ),
    RequiredPrivilege("VM.Clone", "/vms", "Clone the Recovery Lab probe appliance template."),
    RequiredPrivilege(
        "VM.Config.Disk", "/vms", "Adjust disks of restored VMs; move an Instant Recovery VM's disks to storage."
    ),
    RequiredPrivilege(
        "VM.Config.Network", "/vms", "Rewire NICs (and cloud-init IPs) of restored and Recovery Lab VMs."
    ),
    RequiredPrivilege("VM.Config.Options", "/vms", "Set VM options such as enabling the QEMU guest agent."),
    RequiredPrivilege("VM.PowerMgmt", "/vms", "Start and stop restored and Recovery Lab VMs."),
    RequiredPrivilege(
        "VM.GuestAgent.Audit", "/vms", "Recovery Lab level-1 checks: guest-agent ping and reading guest IP addresses."
    ),
    RequiredPrivilege(
        "SDN.Use", "/sdn", "Attach VMs to bridges/VNets (restore targets, isolated Recovery Lab networks)."
    ),
    RequiredPrivilege("SDN.Allocate", "/sdn", "Create and remove the isolated Recovery Lab SDN zone/VNets."),
    RequiredPrivilege("Pool.Allocate", "/pool", "Create the resource pool that groups Recovery Lab VMs."),
    RequiredPrivilege("Pool.Audit", "/pool", "List resource pools and their members (backup-job target picker, Lab)."),
)

# Custom PVE role created (or updated) by RESURGAM's token provisioning and
# granted to the RESURGAM token at `/`.
PVE_ROLE_NAME = "RESURGAM"

# Token name RESURGAM uses when it creates its own token.
DEFAULT_TOKEN_NAME = "resurgam"
TOKEN_COMMENT = "Created by RESURGAM (backup management plane) - do not delete while RESURGAM uses it"


def privilege_names(required: tuple[RequiredPrivilege, ...]) -> list[str]:
    return [p.name for p in required]


def _effective_privileges_for(privileges: dict[str, dict], path: str) -> dict:
    """Longest-prefix match of `path` in the `GET /access/permissions` map.
    Both PVE and PBS return the effective privileges for `/` and the
    standard top-level paths (`/storage`, `/vms`, `/datastore`, ...) plus
    every path that has its own ACL, so the most specific listed ancestor
    is the effective set for `path`.
    """
    candidate = path
    while True:
        if candidate in privileges and isinstance(privileges[candidate], dict):
            return privileges[candidate]
        if candidate == "/":
            return {}
        candidate = candidate.rsplit("/", 1)[0] or "/"


def missing_privileges(privileges: dict[str, dict], required: tuple[RequiredPrivilege, ...]) -> list[str]:
    """Names of every required privilege the token does NOT hold, in the
    order of `required`. `privileges` is the parsed `GET /access/permissions`
    data (`{path: {privilege: propagate_bool}}`); a privilege counts as held
    if it is present at the most specific listed ancestor of its path (the
    value is the propagate bit, not a grant flag, so presence is what counts).
    """
    return [p.name for p in required if p.name not in _effective_privileges_for(privileges, p.path)]


def pve_manual_commands(user_id: str = "resurgam@pve", token_name: str = DEFAULT_TOKEN_NAME) -> list[str]:
    """`pveum` commands (run as root on any PVE node) to set up a token by
    hand. Syntax per pve-docs chapter-pveum ("pveum role add", "pveum user
    token add", "pveum acl modify ... --tokens"). With privilege separation
    the token's effective rights are the intersection of the user's and the
    token's, so the user needs the role too (unless it is root@pam)."""
    privs = " ".join(privilege_names(PVE_REQUIRED_PRIVILEGES))
    full_token_id = f"{user_id}!{token_name}"
    return [
        f'pveum user add {user_id} --comment "RESURGAM service account"',
        f'pveum role add {PVE_ROLE_NAME} --privs "{privs}"',
        f"pveum acl modify / --users {user_id} --roles {PVE_ROLE_NAME}",
        f'pveum user token add {user_id} {token_name} --privsep 1 --comment "{TOKEN_COMMENT}"',
        f"pveum acl modify / --tokens '{full_token_id}' --roles {PVE_ROLE_NAME}",
    ]


def pbs_manual_commands(user_id: str = "resurgam@pbs", token_name: str = DEFAULT_TOKEN_NAME) -> list[str]:
    """`proxmox-backup-manager` commands (run as root on the PBS host) to set
    up a token by hand. Syntax per pbs docs "User Management" ("user
    generate-token", "acl update <path> <role> --auth-id")."""
    full_token_id = f"{user_id}!{token_name}"
    commands = [f"proxmox-backup-manager user create {user_id}"]
    commands.extend(f"proxmox-backup-manager acl update / {role} --auth-id {user_id}" for role in PBS_ROLES)
    commands.append(f"proxmox-backup-manager user generate-token {user_id} {token_name}")
    commands.extend(f"proxmox-backup-manager acl update / {role} --auth-id '{full_token_id}'" for role in PBS_ROLES)
    return commands
