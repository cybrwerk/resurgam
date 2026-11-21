# Builds a generic, deployment-ready RESURGAM appliance qcow2 (manifest
# §5.3's "später automatischer Image-Build als QCOW2"). Starts from
# Debian's own official genericcloud image (cloud-init preinstalled, no
# unattended-installer boot_command needed) and layers on everything
# DEPLOY.md otherwise has an operator do by hand: Docker + the Compose
# stack prebuilt, the native workers' venv, and the four systemd units.
#
# No secrets are baked in -- see files/first-boot.sh. The image is safe
# to store/clone/share; each real boot generates its own.
#
# Run via ./build.sh, not directly -- that script generates the
# build-only SSH keypair and the source tarball this template expects.

packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "src_tarball" {
  type        = string
  description = "Path to a tarball of backend/ restore-worker/ frontend/ appliance/, built by build.sh."
}

variable "disk_size" {
  type    = string
  default = "20000M"
}

variable "memory" {
  type    = number
  default = 2048
}

variable "cpus" {
  type    = number
  default = 2
}

locals {
  # Debian's own official cloud image -- cloud-init preinstalled, so
  # Packer's qemu builder can treat it as a disk_image directly instead
  # of driving an interactive installer. checksum fetched from Debian's
  # own published SHA512SUMS, not hardcoded, so this template self-heals
  # across unrelated point releases of the same image.
  debian_image_url      = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
  debian_image_checksum = "file:https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"
}

source "qemu" "resurgam" {
  iso_url      = local.debian_image_url
  iso_checksum = local.debian_image_checksum
  disk_image   = true
  disk_size    = var.disk_size
  disk_cache   = "writeback"
  format       = "qcow2"

  accelerator = "kvm"
  headless    = true
  memory      = var.memory
  cpus        = var.cpus

  output_directory = "output-resurgam"
  vm_name          = "resurgam.qcow2"

  # Debian's cloud-init NoCloud datasource, not a kickstart/preseed --
  # the base image already has cloud-init, this just gives Packer an SSH
  # user to provision over (ssh_pwauth stays off; key-only, see build.sh).
  cd_label = "cidata"
  cd_files = [
    "${path.root}/http/user-data",
    "${path.root}/http/meta-data",
  ]

  ssh_username         = "resurgam-build"
  ssh_private_key_file = "${path.root}/build_key"
  ssh_timeout          = "15m"
  # The build user is ephemeral: its removal is the very last command the
  # build runs, over its own (already-open) SSH session -- userdel --force
  # works with that session still live, and nothing needs SSH afterwards.
  # The same step resets everything else that must differ per clone:
  # machine-id (systemd derives the DHCP client ID from it, so shared
  # machine-ids mean clones fight over one lease), SSH host keys (regenerated
  # on first boot, see scripts/provision.sh), and cloud-init's own state (so
  # an operator's PVE cloud-init drive is treated as a fresh instance).
  shutdown_command = join(" && ", [
    "sudo sh -c 'rm -f /etc/sudoers.d/90-cloud-init-users",
    "userdel --force --remove resurgam-build",
    "cloud-init clean --logs --machine-id",
    "rm -f /etc/ssh/ssh_host_*",
    "shutdown -h now'",
  ])
}

build {
  sources = ["source.qemu.resurgam"]

  provisioner "file" {
    source      = var.src_tarball
    destination = "/tmp/resurgam-src.tar.gz"
  }

  provisioner "file" {
    source      = "${path.root}/files/first-boot.sh"
    destination = "/tmp/first-boot.sh"
  }

  provisioner "file" {
    source      = "${path.root}/files/resurgam-first-boot.service"
    destination = "/tmp/resurgam-first-boot.service"
  }

  provisioner "shell" {
    script = "${path.root}/scripts/provision.sh"
    # No -E: the script runs as root and must see root's own $HOME
    # (/root), not resurgam-build's -- -E would preserve the SSH user's
    # environment under sudo's elevated privileges, which is exactly the
    # mismatch that made the uv installer put its binaries under
    # /home/resurgam-build/.local/bin instead of /root/.local/bin.
    execute_command = "sudo sh '{{ .Path }}'"
  }
}
