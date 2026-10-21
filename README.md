# Smarthome UI Core

Begleit-Integration zum [Smarthome-UI-Dashboard](https://github.com/derPaul/smarthome-ui). Sie
übernimmt alles, was **laufen soll, während niemand aufs Dashboard schaut**:

- **Mitteilungen** – Push-Nachrichten kommen aus Home Assistant, nicht mehr von einem offenen
  Dashboard („Sender-Gerät“ entfällt).
- **Haushalt & Garten** – Putzplan, Wartung, Gartenkalender, Müll und Mähbereiche liegen hier statt
  im Frontend-Speicher, als Entitäten nutzbar (Automationen, Sprache, andere Dashboards).

Eingestellt wird alles weiter im Dashboard. Ohne diese Integration arbeitet das Dashboard wie bisher.

## Installation

1. In HACS als benutzerdefiniertes Repository (Typ *Integration*) hinzufügen und installieren –
   oder `custom_components/smarthome_ui` nach `/config/custom_components/` kopieren.
2. Home Assistant neu starten.
3. *Einstellungen → Geräte & Dienste → Integration hinzufügen → Smarthome UI Core*.
4. Dashboard öffnen: Es übernimmt einmalig die bisherigen Haushaltsdaten und überträgt die
   Mitteilungs-Einstellungen. Fertig.

## Mitteilungen

Das Dashboard übersetzt seine Erkennung in **Regeln**, die hier ausgewertet werden – rund um die Uhr:

| Regel | Beispiel |
|---|---|
| Zustand (`state`), optional mit Mindestdauer | Fenster seit 10 Minuten offen, Waschmaschine „Fertig“ |
| Änderung (`change`) | Klingel (Ereignis-Entität) |
| Schwelle (`numeric`) | Kühlschrank über 8 °C |
| Template (`template`) | Gerätestatus enthält „fertig“ oder „Störung“ |
| Zeitpunkt (`at`) | Müllabfuhr am Vortag um 12 Uhr – entfällt, wenn schon rausgestellt |
| Kalender (`calendar`) | Termin 30 Minuten vorher, ganztägige am Vortag um 8 Uhr |

Alles, was sich nicht als Regel ausdrücken lässt (z. B. Pakete, Briefkasten, Hinweise einzelner
Seiten), **melden die offenen Dashboards** – jedes offene Gerät zählt, verschickt wird jede Meldung
genau einmal. Eine Mitteilung kommt erneut, wenn ihr Anlass vorbei war (Regeln sofort, gemeldete
Meldungen nach 15 Minuten).

Außerdem:

- **Fällige Aufgaben** (siehe unten) – am Fälligkeitstag zur eingestellten Uhrzeit (Standard 9 Uhr).
- **Dringlich** – iPhone: zeitkritisch (kommt auch im Fokus-Modus), Android: hohe Priorität.
- **Ruhezeiten** – nicht dringliche Mitteilungen werden bis zum Ende der Ruhezeit zurückgehalten.
- **Empfänger** – die im Dashboard gewählten `notify`-Dienste, sonst alle `notify.mobile_app_…`.

| Entität / Aktion | |
|---|---|
| `switch.mitteilungen` | Mitteilungen pausieren (z. B. im Urlaub) |
| `sensor.mitteilungen_zuletzt_gesendet` | Zeitpunkt der letzten Mitteilung, Attribut `recent` mit den letzten 20 |
| `smarthome_ui.test_notify` | Test-Mitteilung an alle Empfänger (auch wenn pausiert) |

## Geräte

Die Integration legt je Bereich ein Dienst-Gerät an. Die Entitäts-IDs richten sich nach dem Gerätenamen
(hier mit deutscher Sprache in Home Assistant):

| Gerät | Entitäten |
|---|---|
| **Mitteilungen** | Schalter zum Pausieren, „Zuletzt gesendet“ |
| **Haushalt** | ein Sensor je Putz- und Wartungsaufgabe, To-do-Liste mit dem Fälligen |
| **Garten** | ein Sensor je Aufgabe aus dem Gartenkalender, To-do-Liste mit dem Fälligen |

Weitere Bereiche (z. B. Sicherheit) kommen als eigene Geräte dazu, sobald sie Entitäten haben.

## Haushalt & Garten

Das Dashboard überträgt, welche Aufgaben es gibt und wann sie fällig sind (Putzen je Raum, Wartung,
Gartenkalender). Daraus entstehen:

| Entität | |
|---|---|
| `sensor.haushalt_<aufgabe>`, `sensor.garten_<aufgabe>` | Fällig am (Zeitstempel), Attribute: zuletzt erledigt, von wem, Intervall, Tage bis fällig |
| `todo.haushalt`, `todo.garten` | Was heute fällig oder überfällig ist – abhaken = erledigt (auch per Sprache) |

| Aktion | |
|---|---|
| `smarthome_ui.task_done` / `task_reopen` | Aufgabe (Name wie im Dashboard, z. B. „Bad putzen“) erledigt / wieder offen |
| `smarthome_ui.waste_ack` / `waste_skip` | Müll am Datum rausgestellt / diesmal nicht |
| `smarthome_ui.area_mowed` | Mähfläche gerade gemäht |

```yaml
# Beispiel: Erinnerung, wenn das Bad seit 2 Tagen überfällig ist
triggers:
  - trigger: template
    value_template: "{{ state_attr('sensor.haushalt_bad_putzen', 'days_left') | int(0) <= -2 }}"
actions:
  - action: notify.mobile_app_handy
    data:
      message: "Das Bad ist überfällig"
```

Wird eine Aufgabe hier erledigt (To-do, Aktion), ist sie sofort im Dashboard abgehakt – und umgekehrt.

## Für Entwickler: WebSocket-API

| Befehl | |
|---|---|
| `smarthome_ui/get` | Daten (`done`, `done_log`, `household`, `mow_log`) und Status |
| `smarthome_ui/subscribe` | Ereignis bei jeder Datenänderung |
| `smarthome_ui/import` | einmalige Datenübernahme aus dem Dashboard |
| `smarthome_ui/done` | Abhaken / wieder öffnen / Rückgängig (`changes`, `log`, `unlog`) |
| `smarthome_ui/data/set` | `household` bzw. `mow_log` ersetzen |
| `smarthome_ui/tasks/sync` | Aufgaben mit Fälligkeit |
| `smarthome_ui/notify/config` | Regeln, Empfänger, Quellen, Ruhezeiten, Uhrzeit für Aufgaben |
| `smarthome_ui/notify/report` | aktuelle Meldungen eines offenen Dashboards |

## Entwicklung

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```
