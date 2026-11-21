# RESURGAM UI/UX Styleguide

**Version:** 1.1  
**Status:** Designgrundlage  
**Basissystem:** cybrwerk **„Petrol & Brass“** (https://claude.ai/artifact/Fevtmk5Qq6LQyGiVSWC6dG) — Farben, Typografie, Abstände, Radien, Top Bar, Side Navigation und Basiskomponenten kommen von dort; dieser Styleguide beschreibt, was RESURGAM darauf aufbaut. Bei Widerspruch gilt Petrol & Brass. Seit 2026-10-04.  
**Projekt:** RESURGAM — A Cyberwerk Project

## 1. Zweck

Dieser Styleguide definiert das visuelle und interaktive Grundsystem für RESURGAM. Er richtet sich an Design, Frontend-Entwicklung und KI-Coding-Agenten und ist als verbindliche Grundlage für Web-App, Appliance-UI und Projektdokumentation gedacht.

RESURGAM ist kein Marketing-Dashboard, sondern ein operatives Backup- und Recovery-Center. Die Oberfläche muss grosse Informationsmengen kompakt darstellen, kritische Zustände unmissverständlich hervorheben und komplexe Restore-Abläufe sicher führen.

## 2. Leitbild

> **Backups are promises. RESURGAM proves recovery.**

Die Gestaltung verbindet drei bewährte Bedienmuster:

- **Proxmox:** permanente hierarchische Navigation, hohe Informationsdichte und unmittelbarer Bezug zwischen ausgewähltem Objekt und verfügbaren Aktionen.
- **PacketFence:** klare Trennung administrativer Arbeitsbereiche, kontextbezogene Menüs und nachvollziehbare Konfigurationspfade.
- **NinjaOne:** schneller operativer Überblick, gut erfassbare Zustände, Filterbarkeit und konsequente Listen- und Detailansichten.

Diese Produkte sind Referenzen für Bedienprinzipien, nicht für eine visuelle Kopie. RESURGAM besitzt ein eigenständiges Cyberwerk-Designsystem.

## 3. Designprinzipien

### 3.1 Operational first

Der wichtigste Zustand steht zuerst: Was ist fehlgeschlagen, was ist gefährdet, wo ist eine Aktion nötig? Dekorative Elemente dürfen operative Informationen niemals verdrängen.

### 3.2 Kontext bleibt sichtbar

Kunde, PBS, Datastore, Namespace und ausgewähltes Backup müssen während einer Aufgabe jederzeit erkennbar bleiben. Der Nutzer darf nie rätseln, auf welchem System eine Aktion ausgeführt wird.

### 3.3 Progressive Offenlegung

Die Oberfläche zeigt zuerst Status und häufige Aktionen. Technische Details, Rohdaten, Task-Logs und seltene Optionen sind erreichbar, aber nicht permanent im Vordergrund.

### 3.4 Sicher vor schnell

Restore, Mount, Prune, Garbage Collection und Änderungen an Credentials benötigen klare Zielangaben, eine verständliche Folgenabschätzung und bei erhöhtem Risiko eine Bestätigung.

### 3.5 Dicht, nicht gedrängt

Listen dürfen kompakt sein. Abstände, Typografie, Spaltenausrichtung und visuelle Hierarchie müssen dennoch ein schnelles Scannen ermöglichen.

### 3.6 Erklären statt verstecken

Fehlertexte nennen Ursache, betroffenes Objekt, Zeitpunkt und eine sinnvolle nächste Handlung. Ein rotes Symbol allein genügt nicht.

## 4. Markenarchitektur

### Produktbezeichnung

- Hauptname: **RESURGAM**
- Descriptor: **Backup & Recovery Center**
- Endorsement: **A Cyberwerk Project**
- Schreibweise im Fliesstext: RESURGAM
- Technische Bezeichner: `resurgam-api`, `resurgam-worker`, `resurgam-agent`

Cyberwerk erscheint zurückhaltend als Herausgeber. In der Appliance steht RESURGAM im Vordergrund. Datimo kann als Founding Sponsor oder Referenzbetreiber genannt werden, ist jedoch nicht Bestandteil des Produktnamens.

### Sprachstil

- präzise, ruhig und technisch
- kurze aktive Sätze
- keine werbliche Übertreibung
- Fachbegriffe verwenden, wenn sie im PBS-/PVE-Umfeld etabliert sind
- Aktionen als Verben benennen: **Restore starten**, **Backup mounten**, **Job erneut ausführen**
- Zustände als klare Aussagen formulieren: **Verifizierung fehlgeschlagen**, nicht **Problem erkannt**

## 5. Farbwelt

Alle Farben sind die Tokens von Petrol & Brass (`frontend/src/styles/tokens.css`, exakte Werte und Verwendungsnotizen im Basissystem). Nur Tokens verwenden, nie Literalwerte.

| Token | Light | Dark | Verwendung |
|---|---:|---:|---|
| `--bg` | `#F4F6F6` | `#0F1718` | Seitenhintergrund |
| `--surface` / `--surface-2` | `#FFFFFF` / `#EEF2F2` | `#172224` / `#1D2A2C` | Panels, Tabellen / Tabellenköpfe, Inputs, Hover |
| `--border` / `--border-strong` | `#D5DDDD` / `#768A8D` | `#2B3B3E` / `#64797C` | Haarlinien / Grenzen von Formularfeldern (3:1) |
| `--text` / `--muted` | `#1C2526` / `#536163` | `#E3E8E8` / `#9FB0B2` | Text / Sekundärtext |
| `--primary` | `#005F6B` | `#5FB7C2` | Petrol: Links, Primärbuttons, aktive Tabs |
| `--nav-bg` / `--nav-bg-2` | `#003A42` / `#00474F` | `#0A2327` / `#0F2F34` | Top Bar und Side Navigation (immer dunkel) |
| `--accent` | `#B08D57` | `#D9B978` | Messing: Fokusring, Navigationskante, Top-Bar-Linie — nicht als Text |
| `--accent-text` | `#7A5C1E` | `#D9B978` | Messing als Text |
| `--ok` / `--warn` / `--crit` (+ `-soft`) | `#2A7549` / `#9A6200` / `#B3261E` | `#63C58F` / `#E3AC4F` / `#F2877D` | Status, immer mit Wort |

Messing ist die einzige Aufmerksamkeitsfarbe. Status nie nur über Farbe: Status-Pill = Punkt + Wort auf `-soft`-Grund.

**White-Label:** Administration → Systemeinstellungen ersetzt `--primary` und `--accent`; die Varianten (Dark-Mode-Töne, `--nav-bg`, `--accent-text`) werden daraus abgeleitet (`BrandingContext`). Ohne Konfiguration gelten die Petrol-&-Brass-Werte; die PDF-Berichte verwenden dieselben Defaults.

**Ältere Token-Namen** (`--surface-panel`, `--text-secondary`, `--color-petrol-700`, `--status-critical` …) sind Aliase auf die Petrol-&-Brass-Tokens; neuer Code verwendet die neuen Namen.

## 6. Typografie

Nach Petrol & Brass:

- UI-Schrift: `--font-sans` (Helvetica Neue / Helvetica, TeX Gyre Heros, Arial)
- Technische Werte, IDs, Hosts, UPIDs: `--font-mono` (IBM Plex Mono), Klasse `mono`, 12.5 px
- Basis: 14 px / 20 px; Bedienelemente 30 px hoch (`--control-height`)

| Stil | Grösse | Gewicht | Verwendung |
|---|---:|---:|---|
| `page-title` | 20 px / 26 | 600 | einmal pro Seite |
| `kpi-value` | 26 px / 32 | 600 | KPI-Kacheln, tabellarische Ziffern |
| `panel-title` | 14 px / 20 | 600 | Panel- und Abschnittstitel |
| `body` | 14 px / 20 | 400 | Standardtext, Tabellenzellen |
| `small` | 13 px / 18 | 400 | Hinweise, Breadcrumbs, Beschreibungen |
| `label` | 12 px / 16 | 600, 0.06em, Grossbuchstaben per CSS | KPI-Beschriftung, Spaltenköpfe |

Überschriften in Satzschreibweise. Schweizer Schreibweise: „ss“, nie „ß“.

## 7. Grundlayout

Nach Petrol & Brass:

1. **Top Bar** (`--topbar-height` 52 px, `--nav-bg`, 2 px Messinglinie unten) über die volle Breite: Logo + Wortmarke links, globale Suche in der Mitte (max. 420 px), rechts Kundenwahl, Sprache (Endonyme), Theme-Umschalter (System → Hell → Dunkel) und Benutzer.
2. **Side Navigation** (`--sidebar-width` 220 px, `--nav-bg`) unter der Top Bar.
3. **Workspace**: Innenabstand 20 px (16 px auf Telefonen), 12 px zwischen Blöcken. Reihenfolge: Breadcrumb → Seitentitel → Filterleiste → KPI-Zeile → Panels → Tabellen.

| Bereich | Wert |
|---|---:|
| Top Bar | 52 px + 2 px Messinglinie |
| Side Navigation | 220 px (RESURGAM-Erweiterung: Icon-Leiste 56 px) |
| Workspace-Innenabstand | 20 px |
| Radius | 4 px (`--radius-md`), Pills 999 px |

Unter 760 px gleitet die Navigation hinter einem Menü-Button ein. CSS nur mit logischen Eigenschaften (`margin-inline-start`, `border-inline-start` …), damit Rechts-nach-links möglich bleibt.

## 8. Menü- und Informationsarchitektur

### 8.1 Hauptnavigation

```text
Übersicht
Infrastruktur
  PBS-Instanzen
  Datastores
  Namespaces
  PVE-Ziele
Backups
  Virtuelle Maschinen
  Container
  Hosts
  Dateisicherung
Jobs
  Backup-Jobs
  Verifizierung
  Prune & Garbage Collection
  Replikation & Sync
Recovery
  Restore Center
  Datei-Recovery
  Disk Mounts
  Recovery Lab
Reporting
  Kapazität & Trends
  Kunden-Usage
  Recovery-Nachweise
Administration
  Kunden
  Benutzer & Rollen
  Credentials
  Integrationen
  Systemeinstellungen
  Audit Log
```

Die Hauptnavigation bleibt auf maximal zwei Ebenen. Tieferliegende Strukturen gehören in den Kontextbaum oder in Tabs einer Detailansicht.

**Verhalten (OPNsense-Prinzip, seit 2026-10-04):**

- Jeder Bereich hat ein Icon; die Seitenleiste ist in beiden Themes dunkel und aus der Primärfarbe abgeleitet (White-Label bleibt wirksam).
- Akkordeon mit genau einem offenen Bereich: standardmässig der Bereich der aktuellen Seite. Ein Klick auf einen anderen Bereich öffnet diesen und schliesst den bisherigen; ein Klick auf den offenen Bereich schliesst ihn.
- Der Bereich der aktuellen Seite trägt immer den Akzentbalken, auch wenn gerade ein anderer Bereich geöffnet ist.
- Die Seitenleiste lässt sich auf eine Icon-Leiste einklappen (pro Benutzer gespeichert); die Einträge eines Bereichs erscheinen dann als Flyout bei Hover oder Tastaturfokus.
- Der Kundenwechsler sitzt in der Kopfleiste, damit er auch bei eingeklappter Seitenleiste sichtbar bleibt.

### 8.2 Navigationsmuster nach Vorbild der Referenzen

#### Proxmox-Prinzip: Objektbaum

Unter **Infrastruktur** zeigt das Context Panel eine Hierarchie:

```text
Alle Systeme
├── PBS-ZRH-01
│   ├── datastore-fast
│   │   ├── root
│   │   ├── customer-azw
│   │   └── customer-hebag
│   └── datastore-archive
└── PBS-WIN-02
    └── datastore-backup
```

Auswahl eines Knotens aktualisiert den Workspace, ohne die Hierarchie zu verlieren. Aktionen beziehen sich immer sichtbar auf den ausgewählten Knoten.

#### PacketFence-Prinzip: Arbeitsbereiche

Administration, Betrieb und Recovery sind getrennte Arbeitsbereiche. Konfigurationsoptionen werden nicht zwischen operative Listen gemischt. Rollen blenden nicht erlaubte Bereiche aus, statt sie nur zu deaktivieren.

#### NinjaOne-Prinzip: Listen als Schaltzentrale

Backup-, Job- und Geräteansichten verwenden filterbare Tabellen mit gespeicherten Views. Ein Klick öffnet ein Detail-Drawer; ein expliziter Link öffnet die vollständige Detailseite. Mehrfachaktionen erscheinen erst nach Auswahl von Zeilen.

### 8.3 Breadcrumbs

Der Seitenkopf zeigt über dem Titel automatisch den Bereich (im Stil `small`), abgeleitet aus dem Navigationsbaum; Detailansichten unter einer Liste zusätzlich den Link zurück zur Liste. Seiten auf oberster Ebene (Übersicht) zeigen keinen, Seiten ausserhalb des Baums übergeben ihre eigenen.

Breadcrumbs zeigen den fachlichen Pfad:

`Infrastruktur / PBS-ZRH-01 / datastore-fast / customer-azw`

Sie ersetzen nicht die Seitentitel. Technische IDs dürfen kopierbar sein.

### 8.4 Seitenaufbau (seit 2026-10-04)

Jede Seite folgt demselben Aufbau, damit sie sich gleich liest:

- **Seitenkopf** (`PageHeader`): Titel = Bezeichnung des Navigationseintrags, ein Satz Beschreibung darunter, die Hauptaktion der Seite (Erstellen, Import, Export, Synchronisieren) rechts im Kopf, nie unterhalb einer Tabelle.
- **Filter** stehen in einer Zeile über der Tabelle (`toolbar`); Export-Aktionen rechtsbündig.
- **Inhaltsblöcke** sind Panels mit Titelleiste (`Panel`); die Aktion eines Blocks (z. B. Bearbeiten) sitzt in dessen Titelleiste. Lesewerte als Schlüssel/Wert-Liste (`detailGrid`), Formulare mit Beschriftung links und Feld rechts (`formGrid`), Abbrechen links von Speichern (`formActions`).
- **Tabs** für Seiten mit drei oder mehr unabhängigen Bereichen; der aktive Tab steht in der URL (`?tab=`).
- **Tabellen** sind gerahmt und kompakt; destruktive Zeilenaktionen bleiben roter Text ohne Rahmen, bis man darauf zeigt. Nebeninformation (z. B. Speicherort) steht als gedämpfte zweite Zeile in der Hauptspalte statt in eigenen Spalten, wenn die Tabelle sonst nicht neben die Seitenleiste passt.
- **Leere Zustände** und „zuerst Kunden wählen“ verwenden `EmptyState`.
- Ein Baum mit genau einem Knoten auf oberster Ebene öffnet diesen von selbst.
- **Begriff:** In der Oberfläche heisst die betreute Organisation auf Deutsch „Kunde“, auf Englisch „Tenant“ (nicht „Mandant“ / „Customer“).

## 9. Dashboard

Das Dashboard beantwortet in dieser Reihenfolge:

1. Gibt es fehlgeschlagene oder überfällige Backups?
2. Sind PBS-Instanzen und Datastores erreichbar und gesund?
3. Droht Kapazitätsmangel?
4. Sind Replikationen und Verify-Jobs aktuell?
5. Wann wurde zuletzt ein Restore erfolgreich nachgewiesen?

Empfohlene Module:

- **Attention Queue:** priorisierte Probleme mit Ursache und Aktion
- **Fleet Health:** PBS-/Datastore-Zustände
- **Job Status 24 h / 7 d:** erfolgreich, Warnung, fehlgeschlagen
- **Storage Forecast:** belegt, Wachstum, prognostiziertes Volllaufdatum
- **Recovery Confidence:** letzter Restore-Test pro Schutzgruppe
- **Recent Activity:** Audit- und Task-Auszug

KPI-Karten dürfen nicht nur Zahlen zeigen. Jede Kennzahl benötigt Zeitraum, Bezugsmenge und Klickziel.

## 10. Tabellen und Listen

Tabellen sind das primäre Arbeitsmittel der App.

### Regeln

- Kopfzeile bleibt beim Scrollen sichtbar.
- Spalten sind sortierbar, aber nicht jede Spalte muss sortierbar sein.
- Filter werden als sichtbare Chips oberhalb der Tabelle dargestellt.
- Häufig verwendete Ansichten können gespeichert werden.
- Spaltenauswahl und Dichte sind benutzerspezifisch speicherbar.
- Statusspalte steht weit links; Aktionsmenü ganz rechts.
- Zeitangaben zeigen lokal verständliche Werte und im Tooltip ISO-Zeitstempel.
- Bytes werden lesbar dargestellt, exakter Wert im Tooltip.
- Leere Zustände erklären, ob keine Daten existieren oder ein Filter nichts findet.

### Standarddichten

- **Comfortable:** 44 px Zeilenhöhe
- **Compact:** 34–36 px Zeilenhöhe
- **Dense:** 30–32 px, nur für Expertenansichten und Logs

## 11. Detailansichten

Jede Ressource verwendet dasselbe Grundmuster:

- Titel mit Name, Typ und Status
- Metadatenzeile mit PBS, Datastore, Namespace und letzter Aktualisierung
- primäre Aktionen rechts
- Tabs darunter

Empfohlene Tabs für eine VM-Sicherung:

1. Übersicht
2. Snapshots
3. Dateien
4. Recovery
5. Jobs
6. Aktivität

Ein Drawer eignet sich für schnelle Prüfung. Komplexe Restore-Abläufe erhalten immer eine eigene Seite oder einen Wizard.

## 12. Aktionen und Buttons

| Variante | Verwendung |
|---|---|
| Primary Petrol | häufigste sichere Aktion der Ansicht |
| Secondary | alternative oder navigierende Aktion |
| Brass | Recovery-bezogene Hauptaktion |
| Danger | destruktive oder schwer rückgängig zu machende Aktion |
| Ghost | Tabellenzeilen, Toolbars, Nebenaktionen |

Pro Bereich höchstens eine visuell primäre Aktion. Icons unterstützen Text, ersetzen ihn bei kritischen Aktionen aber nicht.

Beispiele:

- **Restore vorbereiten** — Messing
- **Job erneut ausführen** — Petrol
- **Mount trennen** — Secondary
- **Snapshot vergessen** — Danger, mit Bestätigung

## 13. Recovery-Wizards

Restore und Recovery Lab verwenden einen linearen Wizard:

1. Quelle auswählen
2. Snapshot und Inhalte prüfen
3. Zielsystem bestimmen
4. Netzwerk und Isolation festlegen
5. Optionen und Konflikte prüfen
6. Zusammenfassung bestätigen
7. Ausführung und Ergebnis

### Pflichtangaben in der Zusammenfassung

- Quell-PBS, Datastore, Namespace und Snapshot
- Ziel-PVE, Storage und VMID
- Netzwerkzuordnung und Isolationsstatus
- Überschreibungen oder Namenskonflikte
- erwartete Datenmenge und Betriebsart
- ausführender Benutzer

Der finale Button benennt die Aktion konkret: **Restore auf PVE-ZRH-02 starten**. Niemals nur **OK** oder **Bestätigen**.

## 14. Task Center

Langlaufende Aktionen verschwinden nicht in modalen Dialogen. Das globale Task Center zeigt:

- laufende, geplante und abgeschlossene Tasks
- Fortschritt und aktuelle Phase
- betroffenes Objekt und Ziel
- Startzeit, Laufzeit und ausführenden Benutzer
- lesbare Fehlermeldung plus Roh-Log
- Korrelation zu PBS-/PVE-Task-ID

Tasks bleiben nach einem Seitenwechsel sichtbar. Kritische Aufgaben können nicht versehentlich doppelt gestartet werden.

## 15. Formulare

- Labels stehen oberhalb des Feldes.
- Pflichtfelder werden textlich gekennzeichnet.
- Validierung erfolgt früh, aber nicht bei jedem Tastendruck störend.
- Secrets werden nie nach dem Speichern erneut vollständig angezeigt.
- Standardwerte sind sichtbar und begründet.
- Erweiterte Optionen sind in einem klar bezeichneten Abschnitt eingeklappt.
- Technische Beispiele stehen direkt beim Feld.

## 16. Feedback, Fehler und leere Zustände

### Toasts

Nur für kurze Bestätigungen wie **Einstellung gespeichert**. Fehler mit Handlungsbedarf gehören inline in den Arbeitskontext oder ins Task Center.

### Fehleraufbau

1. Was ist fehlgeschlagen?
2. Welches Objekt ist betroffen?
3. Wann trat der Fehler auf?
4. Was kann der Nutzer tun?
5. Wo sind technische Details verfügbar?

Beispiel:

> **Verifizierung von `vm/204` fehlgeschlagen.** Der Chunk `a14…` konnte auf `PBS-ZRH-01/datastore-fast` nicht gelesen werden. Prüfe den Datastore-Zustand und starte danach die Verifizierung erneut. [Task-Log öffnen]

## 17. Icons und Visualisierung

- ein konsistentes Outline-Iconset verwenden, beispielsweise Lucide
- Standardgrösse 18–20 px
- Icons für PBS, PVE, Datastore, Namespace, VM, Container und Snapshot eindeutig unterscheiden
- keine Emoji in der produktiven UI
- Donut-Charts nur für einfache Kapazitätsanteile
- Zeitreihen für Wachstum, Laufzeiten und Erfolgsquoten
- Tabellen statt Charts, wenn exakte Einzelwerte entscheidend sind

## 18. Responsive Verhalten

Die Desktop-Oberfläche ist primär für operative Arbeit konzipiert. Tablet und Mobilgerät dienen vor allem zur Zustandsprüfung und Freigabe einfacher Aktionen.

- unter 1024 px: Context Panel als einblendbarer Drawer
- unter 768 px: Primary Sidebar als Off-Canvas-Menü
- Tabellen wechseln nicht pauschal zu Karten; wichtige Spalten bleiben horizontal scrollbar
- komplexe Restore-Wizards bleiben nutzbar, weisen aber bei ungeeigneter Bildschirmgrösse auf Desktop-Nutzung hin

## 19. Barrierefreiheit

- WCAG 2.2 AA als Ziel
- sichtbarer Tastaturfokus mit mindestens 2 px Petrol oder Messing
- vollständige Tastaturbedienung für Navigation, Tabellen und Dialoge
- Kontrastverhältnis mindestens 4.5:1 für normalen Text
- Status nicht nur über Farbe darstellen
- Animationen bei `prefers-reduced-motion` reduzieren
- `aria-live` nur für relevante Statusänderungen einsetzen

## 20. Light und Dark Mode

Beide Modi sind gleichwertig. Der Dark Mode ist nicht lediglich invertierter Light Mode.

- Systempräferenz beim ersten Besuch übernehmen
- Benutzerauswahl speichern
- Charts, Logs und Codeblöcke in beiden Modi prüfen
- Statusfarben müssen auf beiden Hintergründen ausreichend kontrastieren
- Screenshots und Dokumentation bevorzugt im Dark Mode, wenn keine andere Vorgabe besteht

## 21. Komponenten-Tokens

Die Tokens stehen vollständig in `frontend/src/styles/tokens.css` und folgen exakt `tokens.json` von Petrol & Brass. Globale Komponentenklassen (`frontend/src/styles/app.css`) entsprechen dessen Bausteinen: `button`/`buttonPrimary` (pb-btn), `panel`/`panelHeader` (pb-panel), `table` (pb-table), `toolbar` (pb-filterbar), `kpi`/`kpiGrid` (pb-kpi), `tag`, `chip`; `StatusBadge` ist die Status-Pill (pb-status). Destruktive Aktionen bleiben im Standard-Buttonstil und werden in der Seite bestätigt.

## 22. Do / Don't

### Do

- Objektkontext permanent anzeigen.
- Status, Ursache und nächste Aktion gemeinsam darstellen.
- Tabellen für operative Daten verwenden.
- Aktionen nach Häufigkeit und Risiko gewichten.
- Detail- und Rohdaten für Experten erreichbar halten.
- PBS- und PVE-Begriffe konsistent übernehmen.

### Don't

- Keine riesigen KPI-Zahlen ohne Kontext.
- Keine versteckten kritischen Aktionen in unbeschrifteten Icon-Menüs.
- Keine Dashboard-Kacheln als Ersatz für saubere Navigation.
- Keine Modals für langlaufende Tasks.
- Keine ausschliesslich farbliche Statuscodierung.
- Keine optische Kopie von Proxmox, PacketFence oder NinjaOne.
- Keine Marketing-Sprache innerhalb der Appliance.

## 23. Referenzbild für Kernansichten

### Fleet Overview

- Sidebar: **Infrastruktur** aktiv
- Context Panel: Baum aller PBS-Instanzen und Datastores
- Workspace: Health-Zusammenfassung, Attention Queue und Systemtabelle
- Klick auf PBS: Detail-Drawer; Doppelklick oder Link: vollständige Detailseite

### Backup Explorer

- Sidebar: **Backups** aktiv
- Context Panel: Kunde, Systemtyp und Zeitraum als Filter
- Workspace: Snapshot-Tabelle mit Schutzstatus, Grösse, Verify-Alter und Recovery-Aktionen

### Recovery Lab

- Sidebar: **Recovery Lab** aktiv
- Workspace: geplante Tests, laufende Labs und Recovery-Nachweise
- Messing als gezielter Recovery-Akzent
- Testdetails zeigen Quelle, isoliertes Netz, Prüfungen, Ergebnis und Cleanup-Status

## 24. Definition of Done für neue UI-Komponenten

Eine Komponente ist fertig, wenn:

- Light und Dark Mode umgesetzt sind,
- Tastaturbedienung und Fokuszustände funktionieren,
- Loading, Empty, Error und Permission Denied definiert sind,
- kompakte und normale Dichte geprüft wurden,
- Text nicht nur aus Platzhalterbegriffen besteht,
- Kunden- und Objektkontext sichtbar bleiben,
- kritische Aktionen eine angemessene Schutzstufe besitzen,
- Desktop und Tablet getestet wurden,
- das Verhalten im Komponenten-Katalog dokumentiert ist.

---

**Kurzform für KI-Coder:** Baue eine dichte, professionelle Operations-Oberfläche. Nutze eine permanente linke Navigation, optionale hierarchische Kontextbäume, filterbare Tabellen und konsistente Detailansichten. Priorisiere Status, Objektkontext und sichere Recovery-Abläufe. Verwende Petrol als Plattformfarbe, Messing für Recovery und neutrale Flächen für die Arbeitsoberfläche. Vermeide grosse Marketing-Kacheln, unnötige Rundungen und versteckte kritische Aktionen.
