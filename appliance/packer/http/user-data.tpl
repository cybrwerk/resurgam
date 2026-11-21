#cloud-config
# Build-time-only access for Packer's shell/file provisioners. Ephemeral:
# resurgam.pkr.hcl's shutdown_command deletes this user (and its sudoers
# entry) as the build's very last step -- see packer/README.md.
users:
  - name: resurgam-build
    groups: [sudo]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    shell: /bin/bash
    ssh_authorized_keys:
      - __SSH_PUBLIC_KEY__
ssh_pwauth: false
package_update: false
package_upgrade: false
