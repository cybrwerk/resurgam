# RESURGAM Backup Center Appliance – Engineering Manifest

## 1. Auftrag an den KI-Coder

Entwickle eine installierbare virtuelle Appliance mit dem Projektnamen **RESURGAM Backup Center**, die mehrere Proxmox Backup Server (PBS) und Proxmox-VE-Cluster (PVE) zentral verwaltet. Die Appliance ersetzt PBS nicht. Sie bildet eine mandantenfähige Management-, Monitoring-, Reporting- und Restore-Schicht über bestehenden PBS- und PVE-Systemen.

Das Produkt muss zuerst als funktionsfähiger, sicherer MVP entstehen. Implementiere keine Funktionen ausserhalb des definierten Scopes, bevor die Akzeptanzkriterien des MVP erfüllt sind.

## 2. Produktziele

Die Appliance muss:

1. mehrere PBS-Instanzen anbinden können;
2. PBS-Datastores Kunden zuordnen;
3. Backup-, Verify-, Prune-, Garbage-Collection- und Sync-Tasks anzeigen;
4. Datastore-Belegung und deren zeitliche Entwicklung erfassen;
5. gesicherte VMs, Container und Hosts inventarisieren;
6. fehlgeschlagene und überfällige Backups erkennen;
7. OIDC-Anmeldung und mandantenfähiges RBAC unterstützen;
8. Dateien aus PBS-Snapshots durchsuchen und extrahieren;
9. einen vollständigen VM-/CT-Restore zunächst geführt und später automatisiert ermöglichen;
10. sämtliche privilegierten Restore-Aktionen auditieren.

## 3. Grundsätze

- PBS und PVE bleiben die Systeme of Record für Backups und Restore-Tasks.
- Das Backup Center speichert Inventar, Zuordnungen, Messwerte, Richtlinien und Auditdaten, aber keine dauerhaften Kopien von Backup-Inhalten.
- Jeder Kunde erhält vorzugsweise einen eigenen PBS-Datastore auf einem eigenen XFS-Verzeichnis.
- Die physische Datastore-Belegung wird nach Prune und Garbage Collection gemessen.
- Mandantenzuordnung wird serverseitig erzwungen und niemals nur im Frontend gefiltert.
- Die Webanwendung erhält keine Root-Rechte.
- Privilegierte PBS-CLI-Funktionen laufen ausschliesslich über einen separaten Restore Worker.
- Restore-Ziele werden standardmässig nicht gestartet und nicht mit produktiven Netzen verbunden.
- Alle destruktiven und datenlesenden Restore-Aktionen werden protokolliert.

## 4. Zielarchitektur

```text
Browser
  |
  v
Reverse Proxy / TLS
  |
  +--> React Web UI
  |
  +--> FastAPI Control Plane ----> PostgreSQL
              |                         |
              |                         +--> Inventar, Messwerte, RBAC, Audit
              |
              +--> authentik / OIDC
              |
              +--> Secrets Provider / OpenBao
              |
              +--> Job Queue
                       |
                       v
              Native Restore Worker
                 |       |       |
                 |       |       +--> /dev/kvm und optional /dev/fuse
                 |       +----------> PVE REST API
                 +------------------> PBS API und PBS Client CLI
```

### 4.1 Komponententrennung

**Web UI**

- React und TypeScript
- keine direkte Kommunikation mit PBS oder PVE
- alle Autorisierung über die Control Plane

**Control Plane**

- Python und FastAPI
- PostgreSQL über SQLAlchemy und Alembic
- OIDC Authorization Code Flow mit PKCE
- normalisierte PBS-/PVE-Daten
- Policy-, RBAC-, Audit- und Workflow-Engine
- keine Shell-Ausführung und keine Root-Rechte

**Collector**

- kann als unprivilegierter Worker oder Background Service laufen
- liest PBS- und PVE-REST-APIs periodisch aus
- normalisiert Tasks, Snapshots, Datastores und Inventar
- arbeitet idempotent

**Restore Worker**

- läuft nativ als eingeschränkter systemd-Dienst in der Appliance
- führt ausschliesslich allowlist-basierte Befehle aus
- nutzt `proxmox-file-restore` und bei Bedarf `proxmox-backup-client`
- besitzt Zugriff auf `/dev/kvm`; `/dev/fuse` nur bei implementierten Mount-Funktionen
- erhält Jobs über eine authentisierte interne Queue
- akzeptiert keine frei zusammengesetzten Shell-Befehle
- schreibt Restore-Daten nur in ein dediziertes temporäres Dateisystem

### 4.2 Verbindliches API-First-Prinzip

RESURGAM wird API First entwickelt. Die Weboberfläche ist ein Client derselben versionierten API, die auch für Automatisierung und externe Integrationen bereitsteht. Es darf keine exklusive Businesslogik im Frontend geben.

Vorgaben:

- Backend verbindlich mit **Python und FastAPI**;
- REST API unter `/api/v1/`;
- OpenAPI ist Bestandteil des Produkts und wird in CI validiert;
- request- und response-seitig versionierte Pydantic-Modelle;
- keine untypisierten Dictionary-Antworten an öffentlichen Endpunkten;
- einheitliches Fehlerformat mit Code, Meldung, Korrelations-ID und optionalen Details;
- Pagination, Filter, Sortierung und Zeitbereiche konsistent implementieren;
- lange Aktionen asynchron als Jobs mit stabiler Job-ID starten;
- Taskstatus über Polling, Server-Sent Events oder WebSocket bereitstellen, ohne die REST-Schnittstelle zu umgehen;
- Idempotency Keys für wiederholbare Schreiboperationen und Orchestrator-Aktionen;
- RBAC und Customer Scope an jedem API-Endpunkt serverseitig prüfen;
- technische PBS-/PVE-Objekte nicht ungefiltert nach aussen reichen, sondern normalisierte RESURGAM-Modelle verwenden;
- API-Tokens beziehungsweise später OAuth2 Client Credentials für Maschinenzugriff;
- Breaking Changes nur über eine neue API-Version;
- CLI und zukünftige MODEST-Integration verwenden ebenfalls die öffentliche API.

FastAPI, SQLAlchemy, Alembic und PostgreSQL bilden den verbindlichen Backend-Stack. Direkte PBS-/PVE-Zugriffe aus React, Datenbankzugriffe aus Workern ohne definierte Servicegrenzen und Businesslogik in Reverse-Proxy-Skripten sind nicht zulässig.

## 5. Appliance-Modell

### 5.1 Basis

- virtuelle QEMU/KVM-Maschine
- Debian als Basisbetriebssystem, passend zur unterstützten PBS-Client-Version
- UEFI, QEMU Guest Agent und VirtIO-Geräte
- statische IP oder DHCP mit anschliessender Web-Einrichtung
- separate virtuelle System- und temporäre Restore-Disk
- NTP zwingend

Empfohlene Startgrösse:

| Ressource | MVP | Mit parallelen File Restores |
|---|---:|---:|
| vCPU | 4 | 8–12 |
| RAM | 8 GB | 16–32 GB |
| Systemdisk | 64 GB | 100 GB |
| Restore-Cache | 100 GB | nach maximalem Restore-Download |

### 5.2 Nested Virtualization

`proxmox-file-restore` kann für VM-Images eine temporäre QEMU-basierte Restore-Umgebung verwenden. Deshalb muss die Appliance vorzugsweise Hardwarevirtualisierung im Gast erhalten.

Voraussetzungen auf dem PVE-Host:

- Nested Virtualization für Intel VT-x beziehungsweise AMD-V aktiv;
- CPU-Typ der Appliance auf `host`;
- `/dev/kvm` innerhalb der Appliance verfügbar;
- Funktionstest beim Appliance-Setup;
- klare Warnung im UI, wenn nur langsame oder keine VM-Dateiwiederherstellung möglich ist.

Die Appliance darf nicht voraussetzen, dass sie selbst ein PVE- oder PBS-Server ist. Installiert werden nur die benötigten Client-Werkzeuge und Abhängigkeiten.

### 5.3 Packaging

- API, UI, PostgreSQL und optional Queue als Container über Docker Compose oder Podman Compose;
- Restore Worker nativ als systemd-Dienst;
- Reverse Proxy als Container oder nativer Dienst;
- deklarative Konfiguration;
- Appliance-Update als signiertes Release-Bundle;
- Konfigurationsbackup ohne Secrets im Klartext;
- später automatischer Image-Build als QCOW2 und optional OVA.

## 6. Integrationen

### 6.1 PBS

Pro PBS-Instanz speichern:

- Anzeigename und Standort
- Basis-URL und Port
- TLS-Fingerprint oder vertrauenswürdige CA
- API-Token-Referenz aus dem Secrets Provider
- Version und Fähigkeiten
- letzter erfolgreicher Health Check
- zugeordnete Datastores und Kunden

Primär die PBS-REST-API verwenden. Die CLI ist nur für Funktionen vorgesehen, die über die API nicht sinnvoll oder nicht vollständig verfügbar sind.

### 6.2 PVE

Pro PVE-Cluster speichern:

- Anzeigename
- API-Endpunkt
- TLS-Vertrauen
- API-Token-Referenz
- erlaubte Ziel-Storages
- Restore-Netzwerkregeln
- Verbindung zu den verwendeten PBS-Instanzen

PVE wird verwendet, um VM-Namen und Konfigurationen aufzulösen, Zielressourcen zu prüfen und spätere Restore-Jobs auszuführen.

### 6.3 OIDC

Erste Zielintegration: authentik.

Anforderungen:

- Authorization Code Flow mit PKCE
- Prüfung von Issuer, Audience, Signatur, Ablaufzeit und Nonce
- Gruppen- oder Claim-Mapping auf interne Rollen
- lokale Break-Glass-Anmeldung nur explizit, gehärtet und vollständig auditiert
- kurze Sessions und serverseitiger Logout

## 7. Mandanten- und RBAC-Modell

Jeder Benutzer besitzt Rollen und einen Scope.

| Rolle | Fähigkeiten |
|---|---|
| Viewer | Status, Usage und Inventar im eigenen Scope lesen |
| Backup Operator | Logs analysieren und erlaubte Wartungsjobs starten |
| Restore Operator | Dateien durchsuchen und Restores beantragen/ausführen |
| Customer Admin | Benutzer und Sichtbarkeit des eigenen Kunden verwalten |
| Auditor | Auditdaten und Reports read-only anzeigen |
| Platform Admin | Systeme, Tokens, Kunden und globale Policies verwalten |

Scopes:

- global
- Kunde
- PBS-Instanz
- Datastore
- optional PVE-Cluster

Jede Backend-Abfrage muss den effektiven Scope serverseitig berücksichtigen. Direkte Objekt-IDs dürfen keine Autorisierung umgehen.

## 8. Kern-Datenmodell

Mindestens folgende Entitäten implementieren:

```text
users
roles
user_role_assignments
customers
pbs_instances
pve_clusters
datastores
datastore_customer_assignments
backup_groups
backup_snapshots
snapshot_archives
task_runs
sync_jobs
usage_measurements
protection_policies
restore_requests
restore_sessions
audit_events
secret_references
```

Wichtige Regeln:

- technische Fremdschlüssel aus PBS/PVE zusammen mit der Quellinstanz speichern;
- keine API-Token oder Passwörter direkt in PostgreSQL speichern;
- Messwerte append-only erfassen;
- Audit Events nach dem Schreiben nicht änderbar machen;
- Rohantworten nur für Debugging zeitlich begrenzt aufbewahren.

## 9. Funktionsumfang MVP

### 9.1 Systemverwaltung

- PBS-Instanz hinzufügen, testen, deaktivieren und entfernen
- TLS-Vertrauen ausdrücklich bestätigen
- Datastores importieren
- Datastore genau einem Kunden zuordnen
- optional PVE-Cluster anbinden
- Health- und Versionsstatus anzeigen

### 9.2 Dashboard

- letzte erfolgreiche und fehlgeschlagene Tasks
- laufende Tasks
- Backup-, Verify-, Prune-, GC- und Sync-Status
- überfällige Backups
- Datastore-Auslastung
- Kunden- und Systemfilter gemäss RBAC

### 9.3 Usage

Pro Kunden-Datastore periodisch erfassen:

- belegte Bytes
- verfügbare und gesamte Kapazität
- Auslastung in Prozent
- Anzahl Backup-Gruppen nach `vm`, `ct` und `host`
- Anzahl Snapshots
- ältester und jüngster Snapshot
- tägliche und monatliche Veränderung
- Zeitpunkt der letzten erfolgreichen Garbage Collection

Die Appliance muss unterscheiden zwischen:

- PBS-Datastore-Belegung;
- darunterliegender Dateisystembelegung;
- logisch geschützter Datenmenge;
- temporärem Restore-Speicher.

### 9.4 Backup-Inventar

Anzeigen:

- Kunde
- PBS und Datastore
- Namespace
- Backup-Typ
- VMID beziehungsweise Backup-ID
- aus PVE aufgelöster Name, sofern verfügbar
- Snapshots und letzter Backup-Zeitpunkt
- letzter Verify-Status
- Schutzstatus: OK, Warning, Critical, Unknown

### 9.5 Erwartete Backups

Eine Protection Policy definiert:

- erwartetes Intervall
- maximale tolerierte Zeit seit dem letzten erfolgreichen Backup
- erwartete Verify-Frist
- minimale Snapshot-Anzahl
- optionale Sync-/DR-Anforderung

Das System muss fehlende Backups erkennen, auch wenn PBS keinen fehlgeschlagenen Task enthält.

## 10. File Restore

### 10.1 Primärer Mechanismus

Für den Web-Dateibrowser verwenden:

```bash
proxmox-file-restore list <snapshot> <path> \
  --repository '<auth-id>@<server>:<datastore>' \
  --output-format json
```

Für Datei- oder Verzeichnis-Restore verwenden:

```bash
proxmox-file-restore extract <snapshot> <path> <target> \
  --repository '<auth-id>@<server>:<datastore>'
```

Wenn möglich, Downloads über stdout streamen:

```bash
proxmox-file-restore extract <snapshot> <path> - \
  --repository '<auth-id>@<server>:<datastore>'
```

Für Verzeichnisse `zip`, `tar` oder `zstd` anbieten, soweit vom Werkzeug unterstützt.

### 10.2 Interne API

Beispielendpunkte:

```text
GET    /api/v1/snapshots/{id}/files?path=/
POST   /api/v1/snapshots/{id}/extract
GET    /api/v1/restore-sessions
DELETE /api/v1/restore-sessions/{id}
```

`extract` erhält strukturierte Parameter, niemals Shell-Fragmente:

```json
{
  "path": "/drive-scsi0.img/part/1/etc/hosts",
  "format": "plain"
}
```

### 10.3 Session-Lifecycle

Der Worker verwendet:

```bash
proxmox-file-restore status --output-format json
proxmox-file-restore stop <name>
```

Anforderungen:

- maximale Session-Dauer;
- maximale parallele Sessions;
- Abbruchmöglichkeit;
- Cleanup bei Timeout, Fehler und Neustart;
- Quota für temporäre Dateien;
- Audit Event für Browse, Extract, Download und Stop;
- Download-Links kurzlebig und einmalig nutzbar.

### 10.4 Ergänzende Werkzeuge

| Werkzeug | Zweck | MVP |
|---|---|---:|
| `proxmox-file-restore list` | Snapshot durchsuchen | Ja |
| `proxmox-file-restore extract` | Dateien/Verzeichnisse extrahieren | Ja |
| `proxmox-file-restore status/stop` | Sessionsteuerung | Ja |
| `proxmox-backup-client catalog shell` | administrative PXAR-Diagnose | Nein |
| `proxmox-backup-client mount` | PXAR via FUSE mounten | Später |
| `proxmox-backup-client map` | VM-Disk als Blockgerät mappen | Später |

Keine interaktive Shell an das Frontend weiterreichen.

## 11. Vollständiger VM-/CT-Restore

### 11.1 MVP: geführter Restore

Der Benutzer wählt:

- Snapshot
- Ziel-PVE-Cluster und Node
- Ziel-Storage
- neue oder vorhandene VMID
- Netzwerkbehandlung
- Startverhalten

Das System prüft:

- Erreichbarkeit und Berechtigung
- freie VMID
- Zielkapazität
- Sichtbarkeit des PBS-Storage auf dem PVE-Ziel
- Namens- und ID-Konflikte
- Netzwerkgefahr

Danach erzeugt es eine geprüfte Anleitung beziehungsweise ein passendes `qmrestore`- oder `pct restore`-Kommando. Im MVP wird der Restore nicht automatisch gestartet.

### 11.2 Spätere Orchestrierung

- Restore über die PVE-API starten
- UPID speichern und Fortschritt verfolgen
- VM standardmässig ausgeschaltet lassen
- Netzwerkkarten standardmässig trennen oder in ein isoliertes Restore-Netz legen
- Vier-Augen-Freigabe optional erzwingen
- vollständiges Audit Event erzeugen

## 12. Mount- und Mapping-Funktionen

Erst nach dem MVP implementieren.

> **Umsetzungsstand:** Das VM-Disk-Mapping ist seit Version 0.2.4 als «Disk Mounts» umgesetzt
> ([ADR 0025](docs/adr/0025-disk-mounts-helper-vm-share.md)) — abweichend von diesem Abschnitt nicht in
> einem Mount-Namespace auf dem Host, sondern in einer kurzlebigen Helfer-VM auf einem Lab-Node, welche
> die Disks read-only einbindet und per SMB/NFS freigibt (TTL, Cleanup und optionale Vier-Augen-Freigabe
> inklusive). Der PXAR-Mount ist nicht umgesetzt.

### PXAR-Mount

```bash
proxmox-backup-client mount <snapshot> <archive-name> <target>
```

Benötigt FUSE und einen kontrollierten Mount-Namespace.

### VM-Disk-Mapping

```bash
proxmox-backup-client map <snapshot> <archive-name>
```

Anforderungen:

- nur read-only verwenden;
- nur vertrauenswürdige Backups;
- kein automatisches Mounten unbekannter Dateisysteme im Host-Namespace;
- eigener Linux Mount Namespace oder Sandbox pro Job;
- TTL, Unmap und Cleanup erzwingen;
- nie Blockgeräte direkt an Webbenutzer herausgeben.

## 13. File Indexing

Persistentes File Indexing ist kein MVP-Bestandteil.

Der MVP nutzt `proxmox-file-restore list` für On-Demand-Browsing. Eine spätere globale Suche darf pro Kunde optional aktiviert werden und speichert standardmässig nur Metadaten:

- Snapshot-ID
- Backup-Gruppe
- Archiv/Disk
- Pfad und Dateiname
- Typ
- Grösse
- Zeitstempel

Keine Dateiinhalte indexieren. Index-Jobs benötigen I/O-Limits, Zeitfenster, Abbruch, Fortschritt und kundenspezifische Aufbewahrung.

## 14. Sicherheitsanforderungen

- keine Shell mit Benutzereingaben;
- Subprozesse ausschliesslich als Argumentlisten starten;
- Pfade normalisieren und gegen Traversal absichern;
- Snapshot, Datastore und Kunde bei jedem Request erneut validieren;
- Secrets nur zur Laufzeit beziehen;
- API-Tokens mit minimalen Rechten;
- TLS-Zertifikate nicht global ignorieren;
- temporäre Restore-Daten verschlüsselt oder auf ephemerem Storage;
- Download-Dateien mit restriktiven Rechten;
- Rate Limits und maximale Extraktionsgrösse;
- vollständige Auditierung privilegierter Aktionen;
- CSRF-, SSRF-, IDOR- und Command-Injection-Tests;
- Worker darf nur bekannte PBS/PVE-Ziele erreichen;
- Backup-Daten niemals in Applikationslogs schreiben;
- Dateinamen und Pfade in Logs datenschutzgerecht behandeln.

## 15. Interne Worker-Schnittstelle

Die Control Plane sendet typisierte Jobs:

```json
{
  "job_type": "file_restore_list",
  "request_id": "uuid",
  "actor_id": "uuid",
  "customer_id": "uuid",
  "pbs_instance_id": "uuid",
  "datastore_id": "uuid",
  "snapshot_id": "uuid",
  "path": "/",
  "expires_at": "RFC3339"
}
```

Der Worker:

1. validiert Jobtyp und Schema;
2. prüft Ablaufzeit und Signatur;
3. löst technische IDs aus einer serverseitigen Quelle auf;
4. lädt das Secret;
5. führt den allowlist-basierten Prozess aus;
6. begrenzt Laufzeit, CPU, RAM und Output;
7. liefert strukturierte Ergebnisse;
8. räumt temporäre Ressourcen auf;
9. schreibt einen manipulationsgeschützten Abschlussstatus.

## 16. Observability

Bereitstellen:

- strukturierte JSON-Logs
- Prometheus-Metriken
- Health-, Readiness- und Version-Endpunkte
- Zabbix-kompatible Checks
- Korrelations-ID über UI, API, Queue und Worker
- Alarme bei ausgefallenen Collectors, wachsenden Restore-Queues und Cleanup-Fehlern

Keine Tokens, Schlüssel, Dateiinhalte oder vollständigen sensitiven Pfade in Metriken oder Logs.

## 17. Entwicklungsreihenfolge

### Milestone 0 – Technischer Spike

- PBS 4.2.x anbinden
- JSON-Ausgaben der verwendeten API-/CLI-Aufrufe erfassen
- `proxmox-file-restore list`, `extract`, `status` und `stop` in der Appliance testen
- KVM-Verfügbarkeit im nested QEMU-Gast validieren
- Verhalten ohne KVM dokumentieren
- einen VM- und einen CT-/PXAR-Snapshot testen

### Milestone 1 – Control Plane

- Projektstruktur, Datenbank und Migrationen
- OIDC und RBAC
- PBS-Verbindungsverwaltung
- Kunden- und Datastore-Zuordnung
- Collector und normalisiertes Inventar

### Milestone 2 – Operations Dashboard

- Tasks und Logs
- Usage-Historie
- Protection Policies
- Sync-/DR-Status
- Alarmzustände

### Milestone 3 – File Restore MVP

- Dateibrowser
- Einzeldatei- und Verzeichnisdownload
- Restore-Sessions und Cleanup
- Audit Trail
- Limits und Fehlerbehandlung

### Milestone 4 – Guided Restore

- PVE-Anbindung
- Zielprüfung
- generierte Restore-Anleitung
- sichere Netzwerk-Defaults

### Milestone 5 – Appliance Release

- reproduzierbarer QCOW2-Build
- Setup-Wizard
- Backup und Update
- Security Hardening
- Betriebsdokumentation

## 18. Akzeptanzkriterien MVP

Der MVP gilt erst als abgeschlossen, wenn:

1. mindestens zwei PBS-Instanzen gleichzeitig eingebunden sind;
2. ein Benutzer nur die ihm zugewiesenen Kunden sieht;
3. Tasks, Datastores, Snapshots und Backup-Gruppen korrekt synchronisiert werden;
4. die physische Usage je Kunden-Datastore historisiert wird;
5. ein ausbleibendes Backup anhand einer Protection Policy erkannt wird;
6. VM-, CT- und Host-Backups unterscheidbar sind;
7. ein VM-Snapshot im Web durchsucht werden kann;
8. eine einzelne Datei und ein Verzeichnis wiederhergestellt werden können;
9. Restore-Sessions nach Timeout und Neustart bereinigt werden;
10. jede Restore-Aktion Benutzer, Kunde, Snapshot, Pfad, Zeitpunkt und Ergebnis protokolliert;
11. manipulierte Pfade und kundenfremde IDs serverseitig abgewiesen werden;
12. das System ohne KVM einen klaren Capability-Fehler statt eines unkontrollierten Fehlers zeigt;
13. Installation aus einem reproduzierbaren Appliance-Image möglich ist;
14. keine PBS-/PVE-Secrets in Datenbank, Logs oder Frontend erscheinen.

## 19. Explizite Nicht-Ziele des MVP

- PBS selbst neu implementieren
- Backup-Jobs ausserhalb von PBS/PVE ausführen
- persistente Volltextsuche über Dateiinhalte
- direkter Shell-Zugang über das Web
- schreibbarer Mount von Backup-Datenträgern
- automatischer Start restaurierter VMs
- Kubernetes als Voraussetzung
- Abrechnungssystem oder Rechnungsstellung
- Unterstützung fremder Backup-Produkte

## 20. Definition of Done je Feature

Jedes Feature benötigt:

- Migration des Datenmodells
- typisierte API und OpenAPI-Dokumentation
- serverseitige RBAC-Prüfung
- Unit- und Integrationstests
- Fehler- und Timeout-Behandlung
- Audit Events, falls Daten gelesen oder verändert werden
- Observability
- dokumentiertes Threat-Modell bei privilegierten Funktionen
- Bedien- und Betriebsdokumentation
- keine offenen kritischen oder hohen Security Findings

## 21. Erste Implementierungsaufgabe für den KI-Coder

Erstelle zunächst einen technischen Spike, keinen vollständigen UI-Prototyp:

1. Lege die Repository-Struktur für `frontend`, `backend`, `collector`, `restore-worker`, `appliance` und `docs` an.
2. Implementiere einen Capability Check für PBS-Version, CLI-Version, `/dev/kvm` und `/dev/fuse`.
3. Implementiere sichere Wrapper für:
   - `proxmox-file-restore list`
   - `proxmox-file-restore extract`
   - `proxmox-file-restore status`
   - `proxmox-file-restore stop`
4. Verwende niemals `shell=True`.
5. Parse JSON strikt mit versionierten Pydantic-Modellen.
6. Ergänze Integrationstests mit aufgezeichneten, anonymisierten PBS-Antworten.
7. Dokumentiere alle Annahmen, die gegen einer realen PBS-4.2.x-Testinstanz validiert werden müssen.

Erst nach erfolgreichem Spike soll Milestone 1 umgesetzt werden.

## 22. Scheduled Restore Test Orchestrator

Das Backup Center soll nach dem MVP um einen zeitgesteuerten Restore-Test-Orchestrator erweitert werden. Ziel ist nicht nur die Integritätsprüfung eines Backups, sondern ein technischer Nachweis, dass ein ausgewählter Snapshot restauriert, gestartet und auf definierte Dienste geprüft werden kann.

Die zentrale Appliance orchestriert die Abläufe. Die Test-VMs laufen auf einem dedizierten oder ausdrücklich dafür freigegebenen PVE Restore Lab. Die Appliance selbst darf nur für Entwicklung und Demonstration als verschachteltes Restore Lab verwendet werden.

### 22.1 Restore-Testplan

Ein Testplan definiert:

- Kunde und zu prüfende Backup-Gruppen;
- Auswahlregel für Snapshots;
- Ziel-PVE, Node und Restore-Storage;
- isoliertes Testnetz;
- VM-Gruppen, Abhängigkeiten und Startreihenfolge;
- Boot-, Guest-Agent-, Netzwerk- und Applikationstests;
- Zeitplan und maximale Laufzeit;
- Parallelitäts- und Kapazitätsgrenzen;
- Cleanup-Verhalten;
- Benachrichtigungen und Report-Empfänger.

### 22.2 Zustandsmodell

```text
PLANNED
-> SNAPSHOT_SELECTED
-> PREFLIGHT
-> RESTORING
-> NETWORK_ISOLATED
-> STARTING
-> TESTING
-> PASSED | PARTIAL | FAILED
-> CLEANING_UP
-> COMPLETED
```

Jeder Zustandsübergang muss persistent, idempotent und nach einem Neustart wiederaufnehmbar sein. Ein Reaper-Prozess entfernt abgelaufene Test-VMs, Restore-Volumes, Mappings, Overlays und temporäre Netzwerke.

### 22.3 Teststufen

| Stufe | Nachweis |
|---|---|
| Restore | VM-Konfiguration und Disks können wiederhergestellt werden |
| Boot | VM startet und QEMU Guest Agent wird erreichbar |
| Service | definierte TCP-, HTTP-, DNS-, LDAP-, SMB-, SSH- oder Datenbankprüfungen bestehen |
| Application | kundenspezifischer synthetischer Applikationstest besteht |

Ein Lauf darf `PARTIAL` melden, wenn der technische Restore erfolgreich ist, aber ein Service- oder Applikationstest fehlschlägt.

### 22.4 Application Groups

Mehrere VMs müssen als abhängige Applikationsgruppe getestet werden können. Beispiel:

```yaml
application: erp
startup_groups:
  - name: identity
    machines: [dc01, dns01]
    ready_when:
      - guest_agent: available
      - tcp: { host: dc01, port: 389 }
  - name: database
    machines: [sql01]
    ready_when:
      - tcp: { host: sql01, port: 1433 }
  - name: application
    machines: [erp01, web01]
```

### 22.5 Netzwerksicherheit

- pro Testlauf ein eindeutig benanntes isoliertes PVE-SDN-VNet verwenden;
- kein physischer Uplink und kein Routing zum Produktionsnetz;
- Internetzugriff standardmässig sperren;
- optional kontrollierten NAT-Zugriff über eine dedizierte Lab-Firewall erlauben;
- produktive IP- und MAC-Konfiguration nur innerhalb des isolierten Netzes erhalten;
- vor dem VM-Start technisch verifizieren, dass alle NICs dem Testnetz zugeordnet sind;
- bei unsicherem Netzwerkzustand den Lauf abbrechen.

### 22.6 Prüfungen

Der Test Runner soll zunächst folgende typisierte Prüfungen unterstützen:

- QEMU Guest Agent verfügbar;
- erwarteter VM-Status;
- ICMP optional;
- TCP-Port erreichbar;
- HTTP/HTTPS-Statuscode und optional Inhaltsprüfung;
- DNS-Abfrage;
- TLS-Handshake und Zertifikatsprüfung;
- SSH-Banner;
- später LDAP, SMB und Datenbank-Connect;
- später kontrollierte kundenspezifische Skripte oder Playwright-Tests.

Benutzer dürfen keine beliebigen Shell-Kommandos als Test definieren.

### 22.7 Restore-Test-Report

Jeder Lauf erzeugt einen nachvollziehbaren Report mit:

- Kunde und Testplan;
- ausgewählten Snapshots und deren Alter;
- PBS-/PVE-Quelle und Restore-Ziel;
- Restore-, Boot- und Gesamtdauer;
- Ergebnis jeder Prüfung;
- gemessenem Recovery-Zeitwert;
- Fehlern und Timeouts;
- Cleanup-Ergebnis;
- Audit-ID und Softwareversion.

## 23. Speicheroptimierte Restore-Labs

### 23.1 Supported Mode: PVE Live Restore

Der erste produktive Modus verwendet die von PVE unterstützte PBS-Live-Restore-Funktion:

```bash
qmrestore <backup-volume> <vmid> \
  --storage <restore-storage> \
  --live-restore 1
```

Die VM kann starten, während die Daten im Hintergrund auf das Ziel-Storage restauriert werden. Dieser Modus reduziert die Zeit bis zum Boot, benötigt bei vollständigem Durchlauf aber weiterhin Zielkapazität für die restaurierten Disks.

Vor Produktivfreigabe in einem technischen Spike prüfen:

- Verhalten bei frühem kontrolliertem Abbruch;
- Cleanup teilweise restaurierter Volumes;
- Verhalten bei mehreren Disks und Storage-Typen;
- Auswirkung auf PBS, Netzwerk und PVE-Storage;
- belastbare Erkennung von Erfolg, Fehler und Abbruch über UPIDs.

### 23.2 Instant Lab Mode: Read-only Base plus COW Overlay

Ein späterer experimenteller Modus soll funktional dem PowerNFS-/Instant-Recovery-Prinzip entsprechen:

1. PBS-Image read-only mit `proxmox-backup-client map` bereitstellen;
2. pro VM-Disk ein lokales QCOW2-Copy-on-Write-Overlay erzeugen;
3. Test-VM ausschliesslich über die Overlays starten;
4. unveränderte Blöcke direkt aus der PBS-Basis lesen;
5. nur während des Tests veränderte Blöcke lokal speichern;
6. nach dem Test VM, Overlay und Mapping vollständig entfernen.

Der Instant Lab Mode ist nicht Teil des ersten stabilen Releases. Er benötigt einen technischen Machbarkeitsnachweis und eine klar markierte Kompatibilitätsmatrix für PBS-, PVE- und QEMU-Versionen.

### 23.3 Restore Lab Agent

Für den Instant Lab Mode wird ein separater, gehärteter Agent auf einem dedizierten PVE-Lab-Node benötigt. Er ist von der zentralen Appliance getrennt und übernimmt:

- PBS-Image-Mapping;
- lokale COW-Overlays;
- QEMU/KVM- und PVE-Integration;
- isolierte Netzressourcen;
- Limits für parallele Testläufe;
- Cleanup und Reaper;
- signierte Kommunikation mit der Control Plane.

Der Agent soll nicht standardmässig auf Produktions-PVE-Nodes installiert werden.

### 23.4 Gestaffeltes Testmodell

| Rhythmus | Testart | Zusätzlicher Speicherbedarf |
|---|---|---:|
| täglich | Snapshot-SLA und PBS Verify | keiner |
| wöchentlich | Instant Boot mit COW-Overlay | gering |
| monatlich | vollständiger Live Restore | vollständige Nutzdaten |
| quartalsweise | Applikationsgruppe | hoch |
| jährlich | dokumentierter DR-Test | produktionsnah |

## 24. Vorgeschlagene EPICs

### EPIC 0 – Foundation and Appliance

Reproduzierbare Entwicklungsumgebung, Repository-Struktur, Datenbank, CI, QCOW2-Build, Setup-Wizard, Updates, Backup der Appliance und Capability Checks.

**Ergebnis:** Eine installierbare Appliance startet reproduzierbar und meldet ihren Betriebszustand.

### EPIC 1 – Identity and Tenant Security

OIDC mit authentik, Benutzer- und Gruppen-Mapping, Rollen, Customer Scopes, Break-Glass-Verfahren und serverseitige Autorisierung.

**Ergebnis:** Benutzer sehen und bedienen ausschliesslich freigegebene Kundenressourcen.

### EPIC 2 – Multi-PBS Control Plane

Mehrere PBS-Instanzen, TLS-Vertrauen, API-Tokens, Versionserkennung, Capability Matrix, Datastore-Import und Kunden-Zuordnung.

**Ergebnis:** Mindestens zwei PBS-Instanzen können parallel sicher verwaltet werden.

### EPIC 3 – Backup Inventory and Protection State

Inventar für VMs, Container, Hosts, Snapshots und Archive; optionale PVE-Auflösung von VM-Namen; Protection Policies und Erkennung ausbleibender Backups.

**Ergebnis:** Für jedes geschützte System ist erkennbar, ob der erwartete Schutz aktuell erfüllt ist.

### EPIC 4 – Operations and Job Visibility

Backup-, Verify-, Prune-, GC- und Sync-Tasks, Logs, Laufzeiten, Fehler, aktive Tasks, Filter und Alarmzustände.

**Ergebnis:** Der operative PBS-Zustand ist über alle Instanzen zentral sichtbar.

### EPIC 5 – Usage and Capacity Management

Physische und logische Usage, VM-/CT-/Host-Anzahl, historische Messwerte, Wachstum, GC-Bezug, Kapazitätswarnungen und Export.

**Ergebnis:** Usage je Kunden-Datastore ist messbar, historisiert und verrechenbar.

### EPIC 6 – Replication and DR Visibility

PBS Sync Jobs, Quell-/Zielvergleich, Replikationsrückstand, Snapshot-Vollständigkeit, Zeitplan und DR-Status.

**Ergebnis:** Das System zeigt, ob der jüngste erwartete Snapshot am DR-Ziel vorhanden ist.

### EPIC 7 – Granular File Recovery

Web-Dateibrowser über `proxmox-file-restore`, Einzeldatei- und Verzeichnisdownload, Sessionverwaltung, Limits, Cleanup und Auditierung.

**Ergebnis:** Berechtigte Benutzer können Dateien sicher aus VM-, CT- und Host-Snapshots wiederherstellen.

### EPIC 8 – Guided VM and CT Restore

PVE-Anbindung, Zielprüfung, VMID- und Storage-Auswahl, Netzwerkschutz sowie generierte `qmrestore`-/`pct restore`-Anleitung.

**Ergebnis:** Ein Operator erhält einen geprüften und nachvollziehbaren Restore-Plan.

### EPIC 9 – Automated Restore Orchestration

Restore über PVE-API, UPID-Tracking, Fortschritt, Freigaben, sichere Netzwerk-Defaults und automatisches Cleanup.

**Ergebnis:** VMs und Container können kontrolliert und auditiert wiederhergestellt werden.

### EPIC 10 – Scheduled Recovery Verification Lab

Restore-Testpläne, Schedules, Application Groups, Startreihenfolge, isolierte SDN-Netze, technische Prüfungen, Reports und Reaper.

**Ergebnis:** Das System weist regelmässig nach, dass ausgewählte Backups boot- und funktionsfähig sind.

### EPIC 11 – Instant Lab Data Plane

Restore Lab Agent, read-only PBS-Mapping, COW-Overlays, ressourcensparender VM-Start und Kompatibilitätsmatrix.

**Ergebnis:** Regelmässige Boot-Tests benötigen nur Delta-Speicher statt vollständiger Restore-Kapazität.

### EPIC 12 – Reporting, Audit and Community Operations

prüffähige Reports, Audit-Export, Prometheus/Zabbix, Dokumentation, Upgradepfad, anonymisierte Diagnosepakete und Contribution Guidelines.

**Ergebnis:** Die Plattform ist betreibbar, prüfbar und für Community-Beiträge vorbereitet.

## 25. Projektname und Branding

Der festgelegte Projektname lautet:

> **RESURGAM Backup Center**

Der zeitgesteuerte Restore-Test-Orchestrator wird als **RESURGAM Recovery Lab** bezeichnet.

RESURGAM wird als eigenständige Produktmarke unter dem Endorsement **A Cyberwerk Project** geführt. Cyberwerk ist Projekt- und Community-Heimat; die Produktmarke bleibt visuell und funktional eigenständig.

`Resurgam` bedeutet sinngemäss „Ich werde wieder auferstehen“ beziehungsweise „I shall rise again“. Der Name verweist zugleich auf Eric Balls gleichnamige symphonische Dichtung für Brass Band von 1950. Die Bedeutung passt zur zentralen Produktaussage: Ein Backup ist erst belastbar, wenn seine Wiederherstellung nachgewiesen wurde.

Bevorzugte Kurzbeschreibung:

> **Backup operations and recovery verification for Proxmox environments.**

Bevorzugte Produktbotschaft:

> **Backups are promises. RESURGAM proves recovery.**

Bevorzugtes Brand Lockup:

```text
RESURGAM
BACKUP CENTER
A CYBERWERK PROJECT
```

Der Name darf keine offizielle Zugehörigkeit zu Proxmox suggerieren. Website, Repository und Dokumentation müssen RESURGAM ausdrücklich als unabhängiges Community-Projekt beschreiben. Proxmox-Marken und -Logos dürfen nicht Bestandteil des RESURGAM-Logos werden.

### 25.1 Benennung der Komponenten

```text
resurgam-core
resurgam-api
resurgam-web
resurgam-collector
resurgam-worker
resurgam-lab-agent
resurgam-appliance
```

Öffentliches Repository vorzugsweise als `resurgam-backup-center`, falls der kurze Namespace nicht verfügbar ist.

Bevorzugte öffentliche Namespaces:

```text
github.com/cyberwerk/resurgam
ghcr.io/cyberwerk/resurgam-api
ghcr.io/cyberwerk/resurgam-web
ghcr.io/cyberwerk/resurgam-lab-agent
resurgam.cyberwerk.ch
```

### 25.2 Vorläufiger Brandcheck vom 5. September 2026

Der durchgeführte Web- und Namensraumcheck ist eine technische Vorprüfung und keine anwaltliche Markenfreigabe.

Feststellungen:

- keine offensichtliche etablierte Backup-, PBS- oder Recovery-Plattform mit dem exakten Namen `RESURGAM Backup Center` gefunden;
- `RESURGAM` wird von mehreren fachfremden Organisationen und Projekten verwendet;
- ein EU-Industrieprojekt `RESURGAM` arbeitet an Technologien und digitaler Zusammenarbeit für modularen Schiffbau;
- `Resurgam Digital` verwendet den Namen für Design-, Web- und E-Commerce-Dienstleistungen;
- weitere Verwendungen bestehen unter anderem im Kultur-, Bau-, Vereins- und Beratungsumfeld;
- `resurgam.com` ist belegt und zeigt eine generische WordPress-Installation;
- `resurgam.org` ist durch eine Freimaurerloge belegt;
- die Verfügbarkeit von `.io`, `.dev`, `.tech`, `.cloud` und länderspezifischen Domains muss direkt bei einem Registrar geprüft werden;
- Suchmaschinen lieferten keine belastbare Bestätigung für freie oder kollidierende Einträge in den relevanten Markenregistern.

Vor öffentlicher Ankündigung zwingend durchführen:

1. Identitätsrecherche in Swissreg/IGE für die Schweiz;
2. Recherche bei EUIPO/TMview für die EU;
3. WIPO Global Brand Database für internationale Registrierungen;
4. Prüfung mindestens der Nizza-Klassen 9 und 42, je nach Angebot zusätzlich 35, 38 und 45;
5. Ähnlichkeitsrecherche nach `RESURGAM`, `RESURGA`, `RESURGO` und phonetischen Varianten;
6. Verfügbarkeit von GitHub-Organisation, Container Registry, PyPI-/npm-Namespace und Domains verbindlich prüfen;
7. bei kommerziellem Angebot eine schweizerische Markenanwältin oder einen Markenanwalt mit der Freigabe beauftragen.

### 25.3 Vorläufige Risikobewertung

| Bereich | Bewertung | Begründung |
|---|---|---|
| Backup-/Recovery-Software | niedrig bis mittel | keine direkte Kollision gefunden, Registerprüfung offen |
| allgemeine Software-/Digitaldienste | mittel | fachfremde digitale Anbieter verwenden `Resurgam` |
| Domain | mittel bis hoch | `.com` und `.org` bereits belegt |
| Open-Source-Repository | offen | verbindliche Reservierung noch ausstehend |
| Bezug zum Musikwerk | niedrig bei reiner Wortmarke | keine Partitur, Aufnahme, Covergestaltung oder geschützte Gestaltung übernehmen |
| Proxmox-Marken | beherrschbar | unabhängige Integration klar ausweisen und keine offiziellen Logos übernehmen |

Empfehlung: Mit **RESURGAM Backup Center** als qualifiziertem Namen weiterarbeiten, technische Namespaces früh reservieren und vor einer kommerziellen Veröffentlichung eine formelle Markenrecherche durchführen. Bis zur Freigabe den Namen nicht als eingetragene Marke darstellen und kein `®` verwenden.

## 26. UI Design System und Stylesheet

### 26.1 Designrichtung

RESURGAM soll die Informationsarchitektur von GitLab, die operative Statusdarstellung von Wazuh und die technische Dichte von Proxmox aufnehmen, ohne eine dieser Oberflächen zu kopieren.

Leitgedanken:

- Engineering Control Plane statt dekoratives SaaS-Dashboard;
- hohe, aber geordnete Informationsdichte;
- Tabellen und Drill-down vor grossen KPI-Karten;
- Desktop-first, responsive bis Tablet und mobil lesbar;
- vollständiger Light und Dark Mode;
- klare Zustände mit Text, Symbol und Farbe;
- risikoreiche Restore-Aktionen mit geführten Schritten und sicheren Defaults;
- eigenständige Marke ohne Proxmox-Orange, Proxmox-Icons oder nachgebaute Dialoge.

Visuelle Gewichtung:

- GitLab für Navigation, Breadcrumbs, Objektseiten und Tabs;
- Wazuh für Operations-, Event- und Statusansichten;
- Proxmox für kompakte technische Tabellen, Task-Logs und direkte Bedienbarkeit;
- RESURGAM für Farbwelt, Recovery-Timeline und Markenidentität.

Cyberwerk erscheint als zurückhaltendes Endorsement. Das Cyberwerk-Logo wird nicht mit dem RESURGAM-Signet verschmolzen. In kleinen UI-Kontexten darf das Endorsement entfallen; auf Website, Dokumentation, About-Seite und Release-Artefakten wird es sichtbar geführt.

### 26.2 Navigation

```text
RESURGAM
├── Overview
├── Customers
├── Backups
│   ├── Protected Systems
│   ├── Snapshots
│   └── File Recovery
├── Operations
│   ├── Tasks
│   ├── Verification
│   ├── Prune & GC
│   └── Replication
├── Recovery Lab
│   ├── Test Plans
│   ├── Test Runs
│   └── Reports
├── Infrastructure
│   ├── PBS Servers
│   ├── PVE Clusters
│   └── Lab Agents
├── Audit
└── Administration
```

Ein globaler Context Switcher enthält mindestens Kunde, PBS-Instanz und Standort. Der gewählte Scope wirkt auf alle Ansichten und wird vom Backend autorisiert.

Objektseiten folgen dem Muster:

```text
Customer / Protected System / Snapshot
Overview | Snapshots | Jobs | Replication | Restore Tests | Audit
```

### 26.3 Farbpalette

Die Markenfarben verbinden ein tiefes technisches Petrol mit sparsam eingesetztem Messing. Die Richtung lehnt sich atmosphärisch an die Petrolwelt der Optimo Group an, übernimmt aber keine geschützten Gestaltungselemente oder Logos. Messing verweist dezent auf den Brass-Hintergrund von RESURGAM und darf nicht grossflächig für Standardbuttons verwendet werden. Bordeaux bleibt als optionaler Recovery-Akzent verfügbar, ist aber nicht mehr die Primärfarbe.

```css
:root {
  color-scheme: light;

  --resurgam-primary-50: #eff8f8;
  --resurgam-primary-100: #d5eeee;
  --resurgam-primary-300: #75babc;
  --resurgam-primary-500: #197d82;
  --resurgam-primary-600: #0b666d;
  --resurgam-primary-700: #075158;
  --resurgam-primary-900: #043239;

  --resurgam-brass-300: #d2b478;
  --resurgam-brass-500: #b58a45;
  --resurgam-brass-700: #7e5c2c;

  --resurgam-recovery-500: #8a3349;
  --resurgam-recovery-700: #591d2b;

  --resurgam-bg: #f5f6f8;
  --resurgam-surface: #ffffff;
  --resurgam-surface-subtle: #eef0f3;
  --resurgam-text: #202228;
  --resurgam-text-muted: #616672;
  --resurgam-border: #d8dbe1;

  --resurgam-info: #3976b8;
  --resurgam-success: #2f7d4a;
  --resurgam-warning: #a66a16;
  --resurgam-danger: #b33a3a;
  --resurgam-unknown: #737985;

  --resurgam-radius-sm: 4px;
  --resurgam-radius-md: 6px;
  --resurgam-radius-lg: 8px;
  --resurgam-shadow-sm: 0 1px 2px rgb(24 26 31 / 8%);
  --resurgam-focus: 0 0 0 3px rgb(181 138 69 / 35%);
}

[data-theme="dark"] {
  color-scheme: dark;

  --resurgam-bg: #17181b;
  --resurgam-surface: #22242a;
  --resurgam-surface-subtle: #2a2d34;
  --resurgam-text: #f0f1f3;
  --resurgam-text-muted: #afb3bd;
  --resurgam-border: #3a3e47;
  --resurgam-primary-500: #3da3a7;
  --resurgam-primary-600: #268b90;
  --resurgam-brass-500: #c49b57;
  --resurgam-recovery-500: #c4647b;
  --resurgam-info: #64a0dc;
  --resurgam-success: #58a76f;
  --resurgam-warning: #d49a42;
  --resurgam-danger: #df6666;
  --resurgam-unknown: #969ca8;
}
```

Status darf niemals nur über Farbe kommuniziert werden. Jede Darstellung verwendet zusätzlich Icon und Text.

### 26.4 Typografie

- Primärschrift: **IBM Plex Sans**;
- technische Werte: **IBM Plex Mono**;
- Basisgrösse: 14 bis 16 Pixel abhängig von der Ansicht;
- Tabellenwerte und IDs mit tabellarischen Ziffern;
- maximal drei klar unterscheidbare Schriftgewichte;
- keine dekorative Display-Schrift im Applikationsinterface.

Fallback:

```css
font-family: "IBM Plex Sans", Inter, system-ui, sans-serif;
```

Monospace:

```css
font-family: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
```

### 26.5 Layout und Komponenten

- dauerhafte linke Hauptnavigation auf Desktop;
- kompakte Topbar mit Breadcrumb, Context Switcher und Benutzeraktion;
- maximal vier kompakte Kennzahlen auf dem Overview;
- technische Informationen primär in sortierbaren Tabellen;
- rechte Detailleiste oder unteres Task-Log für laufende Operationen;
- Cards nur für echte abgegrenzte Zusammenfassungen;
- keine Karten-in-Karten-Struktur;
- Tabellen besitzen Filter, Spaltenauswahl und gespeicherte Ansichten;
- Destructive Actions verwenden Bestätigungsdialoge mit Objektnamen und Auswirkung;
- Icons ergänzen Beschriftungen, ersetzen sie bei wichtigen Aktionen nicht.

Empfohlene Basis-Komponenten:

```text
AppShell
PrimaryNavigation
ContextSwitcher
Breadcrumbs
PageHeader
StatusSummary
DataTable
FilterBar
ObjectTabs
TaskLogDrawer
Timeline
RecoveryStepper
ConfirmActionDialog
EmptyState
InlineAlert
```

### 26.6 Backup- und Recovery-Status

Vorgesehene Statusbegriffe:

```text
Healthy
Protected
Overdue
Failed
Unverified
Replicating
Restore tested
Running
Unknown
```

Backup, Verify, Replikation und Restore-Test müssen getrennt dargestellt werden:

```text
Backup:       Healthy · 4 hours ago
Verification: Healthy · 2 days ago
Replication:  Healthy · 4 hours ago
Restore test: Overdue · 31 days ago
```

### 26.7 Recovery Lab UI

Der Restore-Testlauf wird als zentrale Ablaufdarstellung gezeigt:

```text
Snapshot -> Preflight -> Restore -> Boot -> Service Tests -> Cleanup
```

Jede Stufe zeigt Status, Startzeit, Dauer, Logzugriff und Fehlergrund. Abhängige Application Groups erhalten eine separate Topologieansicht.

Der Restore-Assistent verwendet folgende Schritte:

1. Snapshot;
2. Restore-Ziel;
3. Netzwerkisolation;
4. Restore-Optionen;
5. Prüfung und Freigabe.

Sichere Defaults:

- VM nach Restore ausgeschaltet;
- keine produktive Netzwerkanbindung;
- neue VMID;
- eindeutiger temporärer Name;
- begrenzte TTL;
- automatisches Cleanup aktiviert.

### 26.8 Accessibility und Qualität

- WCAG 2.2 AA als Ziel;
- vollständige Tastaturbedienung;
- sichtbarer Fokus;
- semantisches HTML;
- ausreichender Farbkontrast in beiden Themes;
- Live-Regionen für Taskfortschritt, ohne jeden Logeintrag vorzulesen;
- reduzierte Animation bei `prefers-reduced-motion`;
- responsive Tabellen mit priorisierten Spalten;
- visuelle Regressionstests für zentrale Ansichten in Light und Dark Mode.

### 26.9 Verbotene Designmuster

- optische Kopie von Proxmox VE;
- Proxmox-Orange als primäre Markenfarbe;
- Glassmorphism, starke Verläufe und dekorative 3D-Effekte;
- riesige Dashboard-Zahlen ohne operativen Kontext;
- Farbe als einziges Statusmerkmal;
- versteckte Restore-Risiken hinter generischen OK-Dialogen;
- unbeschriftete Icons für kritische Aktionen;
- kundenseitige RBAC-Filterung ohne Backend-Prüfung.

## 27. Cyberwerk-Projektmodell und Governance

### 27.1 Projektzuordnung

RESURGAM ist ein unabhängiges Open-Source-Projekt unter dem Dach von Cyberwerk.

Bevorzugte öffentliche Formulierung:

> **RESURGAM is an independent open-source recovery orchestration project initiated by Cyberwerk.**

Datimo IT Solutions kann als Founding Sponsor, erster Referenzbetreiber und kommerzieller Supportpartner auftreten, besitzt aber keine exklusiven Produktfunktionen im offenen Core.

### 27.2 Rollen

| Rolle | Verantwortung |
|---|---|
| Cyberwerk | Projekt- und Markenheimat, Community und Governance |
| Maintainer Team | Architektur, Reviews, Releases und Roadmap |
| Datimo IT Solutions | Founding Sponsor, Referenzbetrieb und Managed Services |
| weitere Organisationen | Entwicklung, Tests, Dokumentation und Integrationen |
| Community | Issues, RFCs, Übersetzungen, Tests und Beiträge |

### 27.3 Trennung von Projekt und Dienstleistung

Offenes Projekt:

```text
RESURGAM Backup Center
A Cyberwerk Project
```

Kommerzielles Betriebsangebot:

```text
Managed RESURGAM
operated by Datimo IT Solutions
```

Mögliche kommerzielle Leistungen:

- Betrieb und Updates der Appliance;
- PBS-/PVE-Integration;
- Monitoring und SLA;
- Planung von Recovery-Tests;
- Compliance- und Recovery-Assurance-Reports;
- kundenspezifische Checks;
- Managed Recovery Lab;
- Support und Engineering.

Die kommerzielle Variante verwendet dieselben offenen APIs und darf die Community-Version nicht künstlich inkompatibel machen.

### 27.4 Governance-Grundsätze

Vor dem ersten öffentlichen Release müssen vorhanden sein:

- öffentliche Roadmap;
- `GOVERNANCE.md`;
- `CONTRIBUTING.md`;
- `CODE_OF_CONDUCT.md`;
- `SECURITY.md` mit Private-Disclosure-Prozess;
- Maintainer- und Reviewregeln;
- öffentlicher RFC-Prozess für grössere Architekturänderungen;
- reproduzierbare und signierte Release-Artefakte;
- Software Bill of Materials;
- dokumentierter Release- und Deprecation-Zyklus;
- transparente Nennung von Sponsoren ohne Produktkontrolle durch Sponsoring.

### 27.5 Lizenzmodell

**Entschieden (2026-09-20): AGPL-3.0-or-later**, einheitlich für das gesamte Repository (`backend/`, `frontend/`, `restore-worker/` inkl. `resurgam-lab-agent`) — siehe [`LICENSE`](./LICENSE). Damit müssen auch als Netzwerkdienst betriebene, modifizierte Versionen ihre Änderungen unter den Lizenzbedingungen verfügbar machen.

Aus der ursprünglichen Prüfliste bewusst nicht separat umgesetzt: eine permissivere Lizenz für einzelne SDKs/Agent-Komponenten (`restore-worker`) — die Frage wurde gestellt und mit einer einheitlichen Lizenz beantwortet, nicht übersehen. Weiterhin offen, nicht Teil dieser Entscheidung: Kompatibilität aller eingebundenen Bibliotheken im Detail, Verteilung der Proxmox-Clientpakete getrennt vom eigenen Source Code, Rechte an Marken/Build-Artefakten/Dokumentation, Auswirkungen auf kommerzielle Integratoren.

Beiträge werden vorzugsweise über ein Developer Certificate of Origin mit `Signed-off-by` angenommen. Ein vollständiger Copyright-Transfer ist nicht als Standard vorgesehen.

### 27.6 Eigentum und Kontinuität

Marke, Domains, Repository-Organisation, Release-Schlüssel und zentrale Infrastruktur sollen durch Cyberwerk oder eine klar definierte juristische Trägerschaft gehalten werden, nicht durch eine Einzelperson.

Mindestens zwei Maintainer verschiedener organisatorischer Zugehörigkeit sollen Release- und Recovery-Zugriffe besitzen. Kritische Aktionen wie Release-Signing, Domaintransfer oder Entfernung von Maintainers benötigen ein dokumentiertes Mehrpersonenverfahren.

### 27.7 Vendor-Neutralität

- keine visuelle Darstellung als offizielles Proxmox-Produkt;
- keine exklusiven Datimo-Endpunkte im offenen Core;
- externe MSPs und Betreiber müssen dieselben Integrationsmöglichkeiten erhalten;
- Plugins und Automatisierungen verwenden die öffentliche RESURGAM-API;
- Hersteller- und Sponsorbeziehungen werden transparent ausgewiesen;
- technische Entscheidungen werden über dokumentierte Anforderungen und RFCs begründet.
