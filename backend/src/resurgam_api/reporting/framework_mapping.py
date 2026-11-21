"""Static editorial metadata mapping each Recovery Evidence report section
to the ISO27001/SOC2/ISO9001 controls it serves as evidence for -- not
database-driven, since this doesn't vary per customer or change at
runtime. Used only when a report is requested with `framework=True`
(`api/v1/reporting.py`); the underlying data/aggregation is identical
either way, this only adds annotation.
"""

from resurgam_api.schemas.reporting import FrameworkControlRef

FRAMEWORK_SECTION_CONTROLS: dict[str, list[FrameworkControlRef]] = {
    "backup_history": [
        FrameworkControlRef(framework="ISO27001:2022", control="A.8.13", title="Information backup"),
        FrameworkControlRef(framework="SOC2", control="CC7.2", title="System monitoring"),
        FrameworkControlRef(
            framework="ISO9001:2015", control="8.5.1", title="Control of production and service provision"
        ),
    ],
    "policy_adherence": [
        FrameworkControlRef(
            framework="ISO27001:2022", control="A.5.29", title="Information security during disruption"
        ),
        FrameworkControlRef(framework="ISO27001:2022", control="A.5.30", title="ICT readiness for business continuity"),
        FrameworkControlRef(framework="SOC2", control="CC7.2", title="System monitoring"),
        FrameworkControlRef(
            framework="ISO9001:2015", control="9.1.1", title="Monitoring, measurement, analysis and evaluation"
        ),
    ],
    "verification_results": [
        FrameworkControlRef(
            framework="ISO27001:2022", control="A.8.13", title="Information backup (restore capability verified)"
        ),
        FrameworkControlRef(framework="SOC2", control="CC7.2", title="System monitoring"),
    ],
    "failed_jobs": [
        FrameworkControlRef(
            framework="ISO27001:2022", control="A.5.24", title="Information security incident management planning"
        ),
        FrameworkControlRef(framework="SOC2", control="CC7.3", title="Incident evaluation and response"),
        FrameworkControlRef(framework="ISO9001:2015", control="10.2", title="Nonconformity and corrective action"),
    ],
    "audit_trail": [
        FrameworkControlRef(framework="ISO27001:2022", control="A.5.15", title="Access control"),
        FrameworkControlRef(framework="ISO27001:2022", control="A.8.15", title="Logging"),
        FrameworkControlRef(framework="ISO27001:2022", control="A.8.16", title="Monitoring activities"),
        FrameworkControlRef(framework="SOC2", control="CC6.1", title="Logical access controls"),
        FrameworkControlRef(framework="SOC2", control="CC7.2", title="System monitoring"),
    ],
    "encryption_attestation": [
        FrameworkControlRef(framework="ISO27001:2022", control="A.8.24", title="Use of cryptography"),
        FrameworkControlRef(framework="SOC2", control="CC6.1", title="Logical access controls"),
        FrameworkControlRef(framework="SOC2", control="CC6.7", title="Transmission and movement of data"),
    ],
    "restore_test_evidence": [
        FrameworkControlRef(framework="ISO27001:2022", control="A.5.30", title="ICT readiness for business continuity"),
        FrameworkControlRef(framework="SOC2", control="CC7.3", title="Incident evaluation and response"),
    ],
    "management_summary": [
        FrameworkControlRef(framework="ISO27001:2022", control="A.5.30", title="ICT readiness for business continuity"),
        FrameworkControlRef(
            framework="ISO9001:2015", control="9.1.1", title="Monitoring, measurement, analysis and evaluation"
        ),
    ],
    "access_control": [
        FrameworkControlRef(framework="ISO27001:2022", control="A.5.15", title="Access control"),
        FrameworkControlRef(framework="ISO27001:2022", control="A.5.18", title="Access rights"),
        FrameworkControlRef(framework="SOC2", control="CC6.1", title="Logical access controls"),
    ],
    "pbs_datastore_access": [
        FrameworkControlRef(framework="ISO27001:2022", control="A.5.15", title="Access control"),
        FrameworkControlRef(framework="ISO27001:2022", control="A.5.18", title="Access rights"),
        FrameworkControlRef(framework="SOC2", control="CC6.1", title="Logical access controls"),
    ],
    "protection_level": [
        FrameworkControlRef(
            framework="ISO27001:2022", control="A.5.29", title="Information security during disruption"
        ),
        FrameworkControlRef(framework="ISO27001:2022", control="A.5.30", title="ICT readiness for business continuity"),
    ],
    "offsite_status": [
        FrameworkControlRef(framework="ISO27001:2022", control="A.8.13", title="Information backup"),
        FrameworkControlRef(framework="SOC2", control="CC7.2", title="System monitoring"),
    ],
    "three_two_one_one_zero": [
        FrameworkControlRef(framework="ISO27001:2022", control="A.8.13", title="Information backup"),
        FrameworkControlRef(framework="SOC2", control="CC7.2", title="System monitoring"),
    ],
}
