#!/usr/bin/env bash
# RESURGAM appliance installer for Proxmox VE.
#
# Run on a PVE node as root:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/cybrwerk/resurgam/main/appliance/pve-install.sh)"
#
# Downloads the appliance image (qcow2) of a RESURGAM release from GitHub,
# checks it against the release's SHA256SUMS, and creates a VM from it:
# q35/OVMF, CPU type host (needed for VM file restores, DEPLOY.md §1),
# virtio-scsi disk, virtio NIC, guest agent, cloud-init for network and an
# optional SSH user. Then starts it and prints the address of the setup
# wizard.
#
# Without a terminal, or with --yes, it uses the defaults below and any
# RESURGAM_* environment variable set (see --help) instead of asking.
set -euo pipefail

REPO="cybrwerk/resurgam"
SCRIPT_URL="https://raw.githubusercontent.com/${REPO}/main/appliance/pve-install.sh"

# --- Defaults (all can be overridden by environment variables) -------------
VERSION="${RESURGAM_VERSION:-latest}"
VMID="${RESURGAM_VMID:-}"
VM_NAME="${RESURGAM_NAME:-resurgam}"
CORES="${RESURGAM_CORES:-4}"
MEMORY_MB="${RESURGAM_MEMORY:-8192}"
DISK_GB="${RESURGAM_DISK:-64}"
STORAGE="${RESURGAM_STORAGE:-}"
BRIDGE="${RESURGAM_BRIDGE:-vmbr0}"
VLAN="${RESURGAM_VLAN:-}"
IP="${RESURGAM_IP:-dhcp}"             # "dhcp" or CIDR, e.g. 192.168.10.20/24
GATEWAY="${RESURGAM_GATEWAY:-}"
DNS="${RESURGAM_DNS:-}"               # space-separated
SEARCHDOMAIN="${RESURGAM_SEARCHDOMAIN:-}"
DOMAIN="${RESURGAM_DOMAIN:-}"         # hostname the UI is served under; empty = the VM's IP
CI_USER="${RESURGAM_CI_USER:-}"       # optional shell user (cloud-init)
SSH_KEY_FILE="${RESURGAM_SSH_KEY_FILE:-}"
ONBOOT="${RESURGAM_ONBOOT:-1}"
START="${RESURGAM_START:-1}"
TMP_BASE="${RESURGAM_TMPDIR:-/var/tmp}"
ASSUME_YES=0

# --- Output helpers ---------------------------------------------------------
if [ -t 1 ]; then
  C_OK=$'\e[32m'; C_WARN=$'\e[33m'; C_ERR=$'\e[31m'; C_DIM=$'\e[2m'; C_B=$'\e[1m'; C_0=$'\e[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_B=""; C_0=""
fi
info() { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*"; }
ok()   { printf ' %s✓%s %s\n' "$C_OK" "$C_0" "$*"; }
warn() { printf ' %s!%s %s\n' "$C_WARN" "$C_0" "$*" >&2; }
die()  { printf ' %s✗ %s%s\n' "$C_ERR" "$*" "$C_0" >&2; exit 1; }

usage() {
  cat <<EOF
RESURGAM appliance installer for Proxmox VE

Usage: pve-install.sh [--yes] [--version vX.Y.Z]

  --yes, -y        Do not ask; use defaults and RESURGAM_* environment variables.
  --version TAG    Install this release (default: the latest release).
  --help           This text.

Environment variables (all optional):
  RESURGAM_VMID, RESURGAM_NAME, RESURGAM_CORES, RESURGAM_MEMORY (MiB), RESURGAM_DISK (GiB),
  RESURGAM_STORAGE, RESURGAM_BRIDGE, RESURGAM_VLAN, RESURGAM_IP (dhcp | CIDR),
  RESURGAM_GATEWAY, RESURGAM_DNS, RESURGAM_SEARCHDOMAIN, RESURGAM_DOMAIN,
  RESURGAM_CI_USER, RESURGAM_SSH_KEY_FILE, RESURGAM_ONBOOT (0/1), RESURGAM_START (0/1),
  RESURGAM_TMPDIR (download directory, default /var/tmp, needs ~4 GB free)

Example (no questions):
  RESURGAM_STORAGE=local-zfs RESURGAM_BRIDGE=vmbr1 RESURGAM_IP=10.0.5.20/24 \\
  RESURGAM_GATEWAY=10.0.5.1 bash -c "\$(curl -fsSL ${SCRIPT_URL})" -- --yes
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --version) VERSION="${2:?--version needs a tag}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
  shift
done

INTERACTIVE=1
if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ] || ! command -v whiptail >/dev/null 2>&1; then
  INTERACTIVE=0
fi
TITLE="RESURGAM appliance installer"

# --- whiptail wrappers (return non-zero on Cancel) --------------------------
ask_input() { # prompt default -> value
  whiptail --title "$TITLE" --inputbox "$1" 10 70 "$2" 3>&1 1>&2 2>&3
}
ask_yesno() { # prompt -> 0 yes / 1 no
  whiptail --title "$TITLE" --yesno "$1" 12 70
}
ask_menu() { # prompt default tag desc ... -> tag
  local prompt=$1 default=$2; shift 2
  whiptail --title "$TITLE" --default-item "$default" --menu "$prompt" 18 74 8 "$@" 3>&1 1>&2 2>&3
}
cancelled() { echo; info "Cancelled, nothing was changed."; exit 0; }

# --- Preflight --------------------------------------------------------------
preflight() {
  [ "$(id -u)" = 0 ] || die "Run as root on a Proxmox VE node."
  command -v pveversion >/dev/null 2>&1 && command -v qm >/dev/null 2>&1 \
    || die "This is not a Proxmox VE node (pveversion/qm not found)."
  [ "$(dpkg --print-architecture)" = amd64 ] || die "The RESURGAM image is amd64 only."
  local major
  major=$(pveversion | sed -n 's#^pve-manager/\([0-9]*\).*#\1#p')
  [ -n "$major" ] && [ "$major" -ge 8 ] || die "Proxmox VE 8 or newer is needed (found: $(pveversion))."
  for tool in curl sha256sum pvesm pvesh; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not found."
  done
}

nested_virt_check() {
  local vendor module param
  if grep -q vmx /proc/cpuinfo; then vendor=intel; module=kvm_intel
  elif grep -q svm /proc/cpuinfo; then vendor=amd; module=kvm_amd
  else warn "This CPU shows no VMX/SVM -- VM file restores in RESURGAM will not work."; return; fi
  param=/sys/module/${module}/parameters/nested
  if [ -r "$param" ] && grep -qE '^(Y|1)$' "$param"; then
    ok "Nested virtualization is on ($vendor)."
    return
  fi
  warn "Nested virtualization is off. RESURGAM needs it to browse and restore files from VM backups."
  local conf=/etc/modprobe.d/${module//_/-}.conf
  if [ "$INTERACTIVE" = 1 ] && ask_yesno "Nested virtualization is off on this node.

RESURGAM needs it to browse and restore files from VM backups (everything else works without).

Write 'options ${module} nested=1' to ${conf}?
It takes effect after a reboot of this node (or reloading ${module} with no VM running). This installer does not reload it."; then
    echo "options ${module} nested=1" > "$conf"
    ok "Wrote $conf -- reboot this node when convenient."
  else
    warn "Left as is. To turn it on later: echo 'options ${module} nested=1' > ${conf} and reboot."
  fi
}

# --- Choices ----------------------------------------------------------------
list_storages() { # name type free-GiB, for storages that hold VM disks
  pvesm status -content images 2>/dev/null | awk 'NR>1 && $3=="active" {printf "%s %s %.0f\n", $1, $2, $6/1048576}'
}
list_bridges() {
  # Every Linux bridge (vmbrX, SDN VNets) except PVE's own per-VM firewall
  # bridges (fwbr*) and Docker's.
  local dev
  for dev in /sys/class/net/*; do
    [ -d "$dev/bridge" ] && basename "$dev"
  done | grep -vE '^(fwbr|docker|br-)' || true
}

pick_storage() {
  local rows default
  rows=$(list_storages)
  [ -n "$rows" ] || die "No active storage for VM disks found (pvesm status -content images)."
  if [ -z "$STORAGE" ]; then
    # Prefer local-lvm / local-zfs, else the one with the most free space.
    default=$(echo "$rows" | awk '$1=="local-lvm"||$1=="local-zfs"{print $1; exit}')
    [ -n "$default" ] || default=$(echo "$rows" | sort -k3 -nr | awk 'NR==1{print $1}')
    STORAGE=$default
  fi
  if [ "$INTERACTIVE" = 1 ]; then
    local args=()
    while read -r name type free; do args+=("$name" "$type, ${free} GiB free"); done <<<"$rows"
    STORAGE=$(ask_menu "Storage for the VM disk:" "$STORAGE" "${args[@]}") || cancelled
  fi
  echo "$rows" | awk -v s="$STORAGE" '$1==s{f=1} END{exit !f}' || die "Storage '$STORAGE' not found or holds no VM disks."
}

pick_bridge() {
  local bridges
  bridges=$(list_bridges)
  [ -n "$bridges" ] || die "No network bridge (vmbrX) found."
  echo "$bridges" | grep -qx "$BRIDGE" || BRIDGE=$(echo "$bridges" | head -n1)
  if [ "$INTERACTIVE" = 1 ]; then
    local args=() b
    while read -r b; do args+=("$b" "$(ip -br -4 addr show "$b" 2>/dev/null | awk '{print $3}')"); done <<<"$bridges"
    BRIDGE=$(ask_menu "Network bridge:" "$BRIDGE" "${args[@]}") || cancelled
  fi
}

valid_cidr() { [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]]; }
valid_ip()   { [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }

ask_network() {
  local choice
  choice=$(ask_menu "IP address of the appliance:" "$([ "$IP" = dhcp ] && echo dhcp || echo static)" \
    dhcp "DHCP" static "Static IP") || cancelled
  if [ "$choice" = static ]; then
    [ "$IP" = dhcp ] && IP=""
    while :; do
      IP=$(ask_input "IP address with prefix (e.g. 192.168.10.20/24):" "$IP") || cancelled
      valid_cidr "$IP" && break
      whiptail --title "$TITLE" --msgbox "'$IP' is not an address like 192.168.10.20/24." 8 60
    done
    while :; do
      GATEWAY=$(ask_input "Gateway:" "${GATEWAY:-${IP%.*}.1}") || cancelled
      valid_ip "$GATEWAY" && break
      whiptail --title "$TITLE" --msgbox "'$GATEWAY' is not an IPv4 address." 8 60
    done
    DNS=$(ask_input "DNS server(s), space-separated (empty = same as this node):" "$DNS") || cancelled
    SEARCHDOMAIN=$(ask_input "Search domain (optional):" "$SEARCHDOMAIN") || cancelled
  else
    IP=dhcp
  fi
  VLAN=$(ask_input "VLAN tag (empty = none):" "$VLAN") || cancelled
}

ask_all() {
  VMID=$(ask_input "VM ID:" "$VMID") || cancelled
  VM_NAME=$(ask_input "VM name (also the appliance's hostname):" "$VM_NAME") || cancelled

  local mode
  mode=$(ask_menu "Settings:" default \
    default "Defaults: ${CORES} cores, $((MEMORY_MB / 1024)) GiB RAM, ${DISK_GB} GiB disk, DHCP" \
    advanced "Advanced: CPU, RAM, disk, network, domain, SSH user") || cancelled

  pick_storage
  pick_bridge

  if [ "$mode" = advanced ]; then
    CORES=$(ask_input "CPU cores:" "$CORES") || cancelled
    MEMORY_MB=$(ask_input "RAM in MiB (8192 = 8 GiB):" "$MEMORY_MB") || cancelled
    DISK_GB=$(ask_input "Disk size in GiB (at least 20):" "$DISK_GB") || cancelled
    ask_network
    DOMAIN=$(ask_input "Hostname the web UI is served under, e.g. resurgam.example.com.
Empty = the VM's IP. A real name is needed for passkeys/security keys; it must resolve to the VM." "$DOMAIN") || cancelled
    CI_USER=$(ask_input "Shell user for SSH (optional, empty = no shell login; the appliance is managed in the browser):" "$CI_USER") || cancelled
    if [ -n "$CI_USER" ]; then
      local guess=""
      for f in /root/.ssh/id_ed25519.pub /root/.ssh/id_rsa.pub /root/.ssh/authorized_keys; do
        [ -s "$f" ] && { guess=$f; break; }
      done
      SSH_KEY_FILE=$(ask_input "File with the SSH public key(s) for ${CI_USER}:" "${SSH_KEY_FILE:-$guess}") || cancelled
    fi
    ask_yesno "Start the VM when this node boots?" && ONBOOT=1 || ONBOOT=0
  fi
}

validate() {
  [[ $VMID =~ ^[0-9]+$ ]] && [ "$VMID" -ge 100 ] || die "VM ID '$VMID' must be a number >= 100."
  if qm status "$VMID" >/dev/null 2>&1 || pct status "$VMID" >/dev/null 2>&1; then
    die "VM ID $VMID is already in use."
  fi
  [[ $VM_NAME =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || die "VM name '$VM_NAME' must be a valid hostname."
  [[ $CORES =~ ^[0-9]+$ ]] && [ "$CORES" -ge 1 ] || die "CPU cores '$CORES' is not a number."
  [[ $MEMORY_MB =~ ^[0-9]+$ ]] && [ "$MEMORY_MB" -ge 2048 ] || die "RAM must be at least 2048 MiB."
  [[ $DISK_GB =~ ^[0-9]+$ ]] && [ "$DISK_GB" -ge 20 ] || die "Disk must be at least 20 GiB."
  [ -z "$VLAN" ] || { [[ $VLAN =~ ^[0-9]+$ ]] && [ "$VLAN" -ge 1 ] && [ "$VLAN" -le 4094 ]; } \
    || die "VLAN tag '$VLAN' must be 1-4094."
  if [ "$IP" != dhcp ]; then
    valid_cidr "$IP" || die "IP '$IP' must be dhcp or an address with prefix, e.g. 192.168.10.20/24."
    valid_ip "$GATEWAY" || die "A static IP needs a gateway (RESURGAM_GATEWAY)."
  fi
  [ -z "$DOMAIN" ] || [[ $DOMAIN =~ ^[A-Za-z0-9.-]+$ ]] || die "Domain '$DOMAIN' is not a hostname."
  if [ -n "$CI_USER" ]; then
    [[ $CI_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "User name '$CI_USER' is not valid."
    [ -s "$SSH_KEY_FILE" ] || die "A shell user needs an SSH public key file (RESURGAM_SSH_KEY_FILE)."
  fi
  ip link show "$BRIDGE" >/dev/null 2>&1 || die "Bridge '$BRIDGE' not found."
}

summary() {
  cat <<EOF
  Release:   ${VERSION}
  VM:        ${VMID} (${VM_NAME}), ${CORES} cores, ${MEMORY_MB} MiB RAM, ${DISK_GB} GiB disk
  Storage:   ${STORAGE}
  Network:   ${BRIDGE}${VLAN:+, VLAN ${VLAN}}, ${IP}${GATEWAY:+ via ${GATEWAY}}${DNS:+, DNS ${DNS}}
  Web UI:    https://${DOMAIN:-<VM IP>}/
  SSH user:  ${CI_USER:-none}
  On boot:   $([ "$ONBOOT" = 1 ] && echo yes || echo no)
EOF
}

# --- Download ---------------------------------------------------------------
resolve_version() {
  if [ "$VERSION" = latest ]; then
    local url
    url=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/${REPO}/releases/latest") \
      || die "Could not reach GitHub to find the latest release."
    VERSION=${url##*/}
    [[ $VERSION =~ ^v[0-9] ]] || die "Could not find the latest release (got '$VERSION')."
  fi
  [[ $VERSION =~ ^v ]] || VERSION="v${VERSION}"
}

WORKDIR=""
cleanup() { if [ -n "$WORKDIR" ]; then rm -rf "$WORKDIR"; fi; }
trap cleanup EXIT

download_image() {
  local base="https://github.com/${REPO}/releases/download/${VERSION}"
  local name="resurgam-${VERSION#v}.qcow2"
  local free_kb
  free_kb=$(df -Pk "$TMP_BASE" | awk 'NR==2{print $4}')
  [ "$free_kb" -ge $((4 * 1024 * 1024)) ] \
    || die "$TMP_BASE has $((free_kb / 1024 / 1024)) GB free; the image needs about 4 GB (set RESURGAM_TMPDIR)."
  WORKDIR=$(mktemp -d "${TMP_BASE%/}/resurgam-install.XXXXXX")

  info "Downloading ${VERSION} from github.com/${REPO}"
  curl -fsSL --retry 3 -o "$WORKDIR/SHA256SUMS" "$base/SHA256SUMS" \
    || die "Release ${VERSION} has no SHA256SUMS -- does it exist? https://github.com/${REPO}/releases"
  local parts
  parts=$(awk -v n="$name" '$2 ~ "^" n "\\.part-[0-9]+$" {print $2}' "$WORKDIR/SHA256SUMS" | sort)
  grep -q " ${name}\$" "$WORKDIR/SHA256SUMS" || die "Release ${VERSION} lists no appliance image (${name})."

  # The image is split into parts (GitHub's 2 GB asset limit); each part is
  # appended to one file, then the whole file is checked.
  : > "$WORKDIR/$name"
  if [ -n "$parts" ]; then
    local part
    for part in $parts; do
      printf '  %s\n' "$part"
      curl -fL --retry 3 --progress-bar "$base/$part" >> "$WORKDIR/$name" || die "Download of $part failed."
    done
  else
    curl -fL --retry 3 --progress-bar -o "$WORKDIR/$name" "$base/$name" || die "Download of $name failed."
  fi

  info "Checking the image against SHA256SUMS"
  (cd "$WORKDIR" && grep " ${name}\$" SHA256SUMS | sha256sum -c --quiet -) \
    || die "Checksum mismatch -- the download is damaged. Nothing was created; run the installer again."
  ok "Checksum OK"
  IMAGE="$WORKDIR/$name"
}

# --- Create the VM ----------------------------------------------------------
remove_half_created_vm() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    warn "Removing the half-created VM ${VMID}"
    qm destroy "$VMID" --purge >/dev/null 2>&1 || true
  fi
  cleanup
  return "$rc"
}

create_vm() {
  local net="virtio,bridge=${BRIDGE},firewall=0${VLAN:+,tag=${VLAN}}"
  local ipcfg="ip=dhcp"
  [ "$IP" = dhcp ] || ipcfg="ip=${IP},gw=${GATEWAY}"
  local desc="RESURGAM appliance ${VERSION} -- https://github.com/${REPO}"

  info "Creating VM ${VMID}"
  qm create "$VMID" \
    --name "$VM_NAME" --description "$desc" --tags resurgam \
    --ostype l26 --machine q35 --bios ovmf \
    --cpu host --cores "$CORES" --memory "$MEMORY_MB" \
    --scsihw virtio-scsi-single --net0 "$net" \
    --agent enabled=1 --onboot "$ONBOOT" >/dev/null
  # Whatever fails from here on leaves a half-made VM: remove it again.
  trap remove_half_created_vm EXIT

  qm set "$VMID" --efidisk0 "${STORAGE}:1,efitype=4m,pre-enrolled-keys=0" >/dev/null
  info "Importing the disk into ${STORAGE}"
  qm set "$VMID" --scsi0 "${STORAGE}:0,import-from=${IMAGE},discard=on,iothread=1,ssd=1" >/dev/null
  qm resize "$VMID" scsi0 "${DISK_GB}G" >/dev/null
  qm set "$VMID" --boot order=scsi0 >/dev/null

  # cloud-init: hostname, network, optional user. The image itself has no
  # login at all (packer/README.md).
  qm set "$VMID" --ide2 "${STORAGE}:cloudinit" --ipconfig0 "$ipcfg" >/dev/null
  [ -z "$DNS" ] || qm set "$VMID" --nameserver "$DNS" >/dev/null
  [ -z "$SEARCHDOMAIN" ] || qm set "$VMID" --searchdomain "$SEARCHDOMAIN" >/dev/null
  if [ -n "$CI_USER" ]; then
    qm set "$VMID" --ciuser "$CI_USER" --sshkeys "$SSH_KEY_FILE" >/dev/null
  fi
  # The VM is complete: from here on, stopping the installer keeps it.
  trap cleanup EXIT
  ok "VM ${VMID} created"
}

guest_exec() { # command... -> stdout of the command in the VM (needs the guest agent)
  qm guest exec "$VMID" --timeout 120 -- "$@" 2>/dev/null \
    | sed -n 's/^ *"out-data" *: *"\(.*\)",\{0,1\}$/\1/p' | sed 's/\\n/\n/g'
}

wait_for_appliance() {
  info "Starting VM ${VMID} and waiting for first boot (a few minutes)"
  qm start "$VMID"
  local addr=""
  for _ in $(seq 1 90); do
    if qm agent "$VMID" ping >/dev/null 2>&1; then
      # first-boot.sh writes .env once Docker and the workers are up.
      if [ "$(guest_exec sh -c 'test -f /opt/resurgam/appliance/.env && echo yes')" = yes ]; then
        addr=$(guest_exec sh -c "hostname -I | awk '{print \$1}'" | head -n1)
        break
      fi
    fi
    sleep 5
  done
  if [ -z "$addr" ]; then
    warn "The appliance did not report back within 7 minutes. Check the VM's console in the PVE web UI."
    return
  fi
  ok "First boot done, IP ${addr}"
  if [ -n "$DOMAIN" ]; then
    info "Setting the web UI address to ${DOMAIN}"
    guest_exec sh -c "sed -i 's/^RESURGAM_DOMAIN=.*/RESURGAM_DOMAIN=${DOMAIN}/' /opt/resurgam/appliance/.env \
      && cd /opt/resurgam/appliance && docker compose up -d >/dev/null 2>&1 && echo done" >/dev/null \
      || warn "Could not set RESURGAM_DOMAIN -- set it in /opt/resurgam/appliance/.env (see DEPLOY.md §4)."
  fi
  URL="https://${DOMAIN:-$addr}/"
}

# --- Main -------------------------------------------------------------------
main() {
  preflight
  printf '\n%sRESURGAM appliance installer%s %s(Proxmox VE %s)%s\n\n' "$C_B" "$C_0" "$C_DIM" "$(pveversion | cut -d/ -f2)" "$C_0"
  nested_virt_check
  [ -n "$VMID" ] || VMID=$(pvesh get /cluster/nextid)

  if [ "$INTERACTIVE" = 1 ]; then
    ask_all
  else
    pick_storage
    pick_bridge
  fi
  validate
  resolve_version

  echo
  summary
  echo
  if [ "$INTERACTIVE" = 1 ]; then
    ask_yesno "Create the RESURGAM appliance with these settings?

$(summary)" || cancelled
  fi

  download_image
  create_vm
  URL=""
  if [ "$START" = 1 ]; then
    wait_for_appliance
  fi

  echo
  ok "${C_B}RESURGAM ${VERSION} is installed as VM ${VMID}.${C_0}"
  if [ -n "$URL" ]; then
    echo "   Open ${C_B}${URL}${C_0} to run the setup wizard and create the first admin."
    echo "   The certificate is self-signed until you set up your own (see the operator manual)."
  elif [ "$START" = 1 ]; then
    echo "   Once it is up: open https://<VM IP>/ for the setup wizard."
  else
    echo "   Start it with: qm start ${VMID}, then open https://<VM IP>/ for the setup wizard."
  fi
  [ -z "$DOMAIN" ] || echo "   ${DOMAIN} must resolve to the VM's IP (DNS or hosts file)."
  echo "   Updates: System Settings -> Updates in the web UI."
}

main
