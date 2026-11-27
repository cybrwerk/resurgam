"""The Zabbix template RESURGAM ships (`zabbix_template.yaml`, Zabbix 7.0
export format) and its download with the appliance's own address filled in.

The template polls the read-only `/api/v1/zabbix/*` endpoints (one master
HTTP item per topic, per-entity dependent items via JSONPath -- the pattern
of Zabbix's own official HTTP templates) plus `/api/v1/readiness`.
"""

from __future__ import annotations

from importlib import resources

TEMPLATE_NAME = "RESURGAM by HTTP"
TEMPLATE_FILENAME = "zabbix_template_resurgam.yaml"
MIN_ZABBIX_VERSION = "7.0"
# The template's own revision, `vendor.version` in the YAML -- bump both together.
TEMPLATE_REVISION = "0.3-3"
_URL_PLACEHOLDER = "value: 'https://resurgam.example.com'"


def template_yaml() -> str:
    return resources.files(__package__).joinpath("zabbix_template.yaml").read_text(encoding="utf-8")


def render_template(appliance_url: str) -> str:
    """The template with `{$RESURGAM.URL}` preset to `appliance_url` (the
    address Zabbix must use), so only the token is left to enter."""
    text = template_yaml()
    url = appliance_url.rstrip("/").replace("'", "''")
    if text.count(_URL_PLACEHOLDER) != 1:
        raise RuntimeError("zabbix_template.yaml: the {$RESURGAM.URL} default value is missing or not unique")
    return text.replace(_URL_PLACEHOLDER, f"value: '{url}'")
