from __future__ import annotations

import logging
import time

from resurgam_worker.lab_prober.checks import run_check
from resurgam_worker.lab_prober.client import LabProberApiError, LabProberClient, ProbeResult, ProbeTarget
from resurgam_worker.lab_prober.config import LabProberConfig, load_config
from resurgam_worker.lab_prober.netinit import add_alias, alias_for_target, needs_alias

logger = logging.getLogger("resurgam_worker.lab_prober")


def process_target(target: ProbeTarget, config: LabProberConfig, client: LabProberClient) -> None:
    """Runs every requested network-tier check against one member and
    posts the results back -- merged, not replaced, on the backend side
    (`submit_probe_results`), so a target that reappears on a later poll
    (e.g. one check type resolved this cycle, another still pending)
    doesn't clobber an already-recorded result.

    If the target's IP falls outside this appliance's own `net0` subnet
    (a member with a baked-in static production IP, ADR 0019 Phase B),
    a secondary /32 alias is added on `net0` first -- purely a change to
    this VM's own interface, no PVE/host involvement.
    """
    if needs_alias(config.own_subnet_cidr, target.ip):
        add_alias(config.net0_interface, alias_for_target(target.ip))

    results = [ProbeResult.model_validate(run_check(check_type, target.ip)) for check_type in target.check_types]
    client.submit_probe_results(target.member_id, results)
    logger.info(
        "lab-prober: submitted %d result(s) for member %s (%s)",
        len(results),
        target.member_id,
        ", ".join(f"{r.check_type}={r.status}" for r in results),
    )


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    client = LabProberClient(config.backend_url, config.run_id, config.run_token)
    logger.info(
        "resurgam-lab-prober starting for run %s (poll interval %.1fs)", config.run_id, config.poll_interval_seconds
    )

    while True:
        try:
            targets = client.list_probe_targets()
        except LabProberApiError as exc:
            logger.warning("lab-prober: could not reach RESURGAM backend: %s", exc)
            time.sleep(config.poll_interval_seconds)
            continue

        for target in targets:
            try:
                process_target(target, config, client)
            except LabProberApiError as exc:
                logger.info("lab-prober: skipping target for member %s: %s", target.member_id, exc)

        time.sleep(config.poll_interval_seconds)


if __name__ == "__main__":
    run_forever()
