"""Printable key sheet for a PBS encryption key (ADR 0031, stage 2).

One A4 page: the fingerprint, the full keyfile as text and as a QR code, and
how to restore it. Printed and filed away, it is the copy of the key that
survives losing RESURGAM, PVE and every disk the keyfile was on -- the same
idea as PBS's own `proxmox-backup-client key paperkey`.
"""

import base64
import io
from datetime import datetime

import segno
from jinja2 import Environment
from weasyprint import HTML

_TEMPLATE = Environment(autoescape=True).from_string(
    """<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page { size: A4; margin: 18mm; }
  body { font-family: sans-serif; font-size: 10pt; color: #1d2a2e; }
  h1 { font-size: 16pt; color: #005f6b; margin: 0 0 4mm; }
  .warn { border: 1px solid #b08d57; background: #f7f1e6; padding: 3mm; margin: 4mm 0; }
  table { border-collapse: collapse; margin: 3mm 0; }
  td { padding: 1mm 4mm 1mm 0; vertical-align: top; }
  td.k { color: #5a6b70; }
  .fp { font-family: monospace; font-size: 11pt; word-break: break-all; }
  pre { font-family: monospace; font-size: 7.5pt; white-space: pre-wrap; word-break: break-all;
        border: 1px solid #c8d2d4; padding: 3mm; }
  .qr { text-align: center; margin: 4mm 0; }
  .qr img { width: 75mm; height: 75mm; }
  ol { padding-left: 5mm; }
</style></head><body>
  <h1>Backup encryption key</h1>
  <div class="warn"><strong>Keep this sheet locked away.</strong> Anyone who has it can read every backup
    encrypted with this key. Without this key, those backups cannot be restored by anyone.</div>
  <table>
    <tr><td class="k">Fingerprint</td><td class="fp">{{ fingerprint }}</td></tr>
    {% if label %}<tr><td class="k">Label</td><td>{{ label }}</td></tr>{% endif %}
    {% if customer %}<tr><td class="k">Tenant</td><td>{{ customer }}</td></tr>{% endif %}
    {% if key_created %}<tr><td class="k">Key created</td><td>{{ key_created }}</td></tr>{% endif %}
    <tr><td class="k">Sheet printed</td><td>{{ printed }} by {{ printed_by }}</td></tr>
  </table>
  <div class="qr"><img src="{{ qr }}" alt="QR code of the keyfile"></div>
  <p>The QR code contains the complete keyfile below.</p>
  <pre>{{ keyfile }}</pre>
  <p><strong>To restore the key</strong></p>
  <ol>
    <li>Scan the QR code (or type the text above) into a file, e.g. <code>backup.key</code>.</li>
    <li>Check it: <code>proxmox-backup-client key show backup.key</code> must print the fingerprint above.</li>
    <li>Use it: upload it in RESURGAM under Administration &rarr; Encryption keys, set it as the PVE
      storage's encryption key, or pass it to <code>proxmox-backup-client --keyfile backup.key</code>.</li>
  </ol>
</body></html>"""
)


def _qr_data_uri(content: str) -> str:
    buffer = io.BytesIO()
    # Medium error correction: still readable with a crease or a stain, and a
    # PVE keyfile (~500 bytes) stays around QR version 17.
    segno.make(content, error="m", micro=False).save(buffer, kind="svg", scale=4, border=2)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def render_key_sheet_pdf(
    *,
    fingerprint: str,
    keyfile: str,
    label: str,
    customer_name: str | None,
    key_created_at: datetime | None,
    printed_at: datetime,
    printed_by: str,
) -> bytes:
    html = _TEMPLATE.render(
        fingerprint=fingerprint,
        keyfile=keyfile,
        label=label,
        customer=customer_name,
        key_created=key_created_at.strftime("%Y-%m-%d %H:%M UTC") if key_created_at else None,
        printed=printed_at.strftime("%Y-%m-%d %H:%M UTC"),
        printed_by=printed_by,
        qr=_qr_data_uri(keyfile),
    )
    return HTML(string=html).write_pdf()
