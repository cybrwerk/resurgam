#!/bin/sh
# Builds the RESURGAM appliance qcow2. Run from anywhere; everything is
# resolved relative to this script's own location.
#
# Requires: packer, qemu-system-x86_64/qemu-img, /dev/kvm access
# (accelerator = "kvm" in resurgam.pkr.hcl -- the build itself does not
# need nested virtualization, only the deployed appliance does).
set -eu

cd "$(dirname "$0")"
repo_root="../.."

if [ ! -f build_key ]; then
  ssh-keygen -t ed25519 -N "" -f build_key -C resurgam-packer-build -q
fi

sed "s#__SSH_PUBLIC_KEY__#$(cat build_key.pub)#" http/user-data.tpl > http/user-data

src_tarball="$(mktemp -t resurgam-src.XXXXXX.tar.gz)"
# The same source pack an update bundle ships (release/pack-source.sh):
# appliance/packer is excluded outright -- the image never needs it (files/
# is delivered by Packer's own file provisioners), and it holds the
# previous build's multi-GB qcow2 plus the build's private SSH key, neither
# of which may end up inside the image -- and so are .env files.
sh ../release/pack-source.sh "$repo_root" "$src_tarball"

cleanup() { rm -f "$src_tarball"; }
trap cleanup EXIT

# Packer refuses to run if a previous build's output is still there.
rm -rf output-resurgam

packer init resurgam.pkr.hcl
packer build -var "src_tarball=${src_tarball}" resurgam.pkr.hcl

echo "Built: $(pwd)/output-resurgam/resurgam.qcow2"
