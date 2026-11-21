"""Recovery Lab compute provisioning driven by RESURGAM itself: the lab
packages shipped with the backend image (`artifacts`), the uplink network
of the probe appliance (`uplink`), and the guided, one-time SSH flows that
install `resurgam-lab-agent` on a lab node and build the probe appliance
template there (`ssh`, `jobs`, `agent_install`, `template_build`).
"""
