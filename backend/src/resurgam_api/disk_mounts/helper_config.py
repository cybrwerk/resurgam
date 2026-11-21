"""Renders what a Disk Mounts helper VM is configured with (ADR 0025).

- `disk_serial(index)`: the QEMU drive serial each mapped disk gets
  (qm.conf `serial`: "The drive's reported serial number, url-encoded, up to
  20 bytes long") so the helper finds the backup's disks by serial, not by
  guessed device order.
- `render_helper_config(...)`: the JSON the in-guest script reads. Contains no
  secret: the SMB password is set after boot through
  `qm guest exec --pass-stdin` (qm(1)), never through cloud-init.
- `render_user_data(...)`: the `#cloud-config` written as the helper's
  `cicustom` user snippet (pve-docs Cloud-Init Support: "The custom config
  files have to be on a storage that supports snippets").
- `helper_nic(...)` / `helper_ipconfig(...)`: the `net0` / `ipconfig0` values
  (apidoc.js `net[n]` `tag=` 1-4094; `ipconfig[n]` `ip=dhcp` or
  `ip=<IPv4/CIDR>[,gw=<IPv4>]`).
"""

import base64
import json
from pathlib import Path

GUEST_SCRIPT_PATH = Path(__file__).with_name("share_helper_guest.py")
GUEST_SCRIPT_INSTALL_PATH = "/usr/local/sbin/resurgam-share-helper"
GUEST_CONFIG_PATH = "/etc/resurgam-share-helper/config.json"
GUEST_RESULT_PATH = "/run/resurgam-share-helper/result.json"
SERIAL_PREFIX = "rsgm"


def disk_serial(index: int) -> str:
    """`rsgm1`, `rsgm2`, ... -- short, url-safe, far below the 20-byte limit."""
    return f"{SERIAL_PREFIX}{index}"


def guest_script() -> str:
    """The helper script as installed in the guest, with a shebang so it also
    runs when executed directly (the agent calls it through python3 anyway)."""
    return "#!/usr/bin/python3\n" + GUEST_SCRIPT_PATH.read_text(encoding="utf-8")


def render_helper_config(
    *, mount_id: str, smb_user: str | None, protocols: list[str], allowed_clients: list[str], archives: list[str]
) -> dict:
    return {
        "mount_id": mount_id,
        "smb_user": smb_user,
        "protocols": list(protocols),
        "allowed_clients": list(allowed_clients),
        "disks": [
            {"index": i, "serial": disk_serial(i), "archive": archive} for i, archive in enumerate(archives, start=1)
        ],
    }


def render_user_data(config: dict, *, hostname: str) -> str:
    """cloud-init user data: write the config + the helper script, run setup.
    Both files are embedded base64 (cloud-init `write_files` `encoding: b64`) so
    no YAML quoting rule can ever mangle them."""
    config_b64 = base64.b64encode(json.dumps(config, indent=2).encode()).decode()
    script_b64 = base64.b64encode(guest_script().encode()).decode()
    return (
        "#cloud-config\n"
        "# RESURGAM Disk Mounts helper (ADR 0025). Contains no credentials.\n"
        f"hostname: {hostname}\n"
        "ssh_pwauth: false\n"
        "write_files:\n"
        f"  - path: {GUEST_CONFIG_PATH}\n"
        "    permissions: '0600'\n"
        "    encoding: b64\n"
        f"    content: {config_b64}\n"
        f"  - path: {GUEST_SCRIPT_INSTALL_PATH}\n"
        "    permissions: '0755'\n"
        "    encoding: b64\n"
        f"    content: {script_b64}\n"
        "runcmd:\n"
        f"  - [/usr/bin/python3, {GUEST_SCRIPT_INSTALL_PATH}, setup, {GUEST_CONFIG_PATH}]\n"
    )


def helper_nic(bridge: str, vlan_tag: int | None) -> str:
    value = f"model=virtio,bridge={bridge}"
    if vlan_tag is not None:
        value += f",tag={vlan_tag}"
    return value


def helper_ipconfig(ip_mode: str, address_cidr: str | None, gateway: str | None) -> str:
    if ip_mode == "dhcp":
        return "ip=dhcp"
    if not address_cidr:
        raise ValueError("a static helper address is required")
    value = f"ip={address_cidr}"
    if gateway:
        value += f",gw={gateway}"
    return value
