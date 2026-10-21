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
| `switch.notifications` | Mitteilungen pausieren (z. B. im Urlaub) |
| `sensor.notifications_last_sent` | Zeitpunkt der letzten Mitteilung, Attribut `recent` mit den letzten 20 |
| `smarthome_ui.test_notify` | Test-Mitteilung an alle Empfänger (auch wenn pausiert) |

## Geräte

Die Integration legt je Bereich ein Dienst-Gerät an. Die Namen folgen der Sprache von Home Assistant
(„Garten“, „Mitteilungen“ …), die Entitäts-IDs sind immer englisch – so passen Automationen und Beispiele
überall:

| Gerät | Entitäten |
|---|---|
| **Mitteilungen** | `switch.notifications` (pausieren), `sensor.notifications_last_sent` |
| **Haushalt** | `sensor.household_<aufgabe>` je Putz- und Wartungsaufgabe, `todo.household` |
| **Garten** | `sensor.garden_<aufgabe>` je Aufgabe aus dem Gartenkalender, `todo.garden` |

`<aufgabe>` kommt aus dem Namen im Dashboard, z. B. `sensor.garden_hecke_schneiden`. Umbenennen in
Home Assistant ist jederzeit möglich.

Weitere Bereiche (z. B. Sicherheit) kommen als eigene Geräte dazu, sobald sie Entitäten haben.

## Haushalt & Garten

Das Dashboard überträgt, welche Aufgaben es gibt und wann sie fällig sind (Putzen je Raum, Wartung,
Gartenkalender). Daraus entstehen:

| Entität | |
|---|---|
| `sensor.household_<aufgabe>`, `sensor.garden_<aufgabe>` | Fällig am (Zeitstempel), Attribute: zuletzt erledigt, von wem, Intervall, Tage bis fällig |
| `todo.household`, `todo.garden` | Was heute fällig oder überfällig ist – abhaken = erledigt (auch per Sprache) |

| Aktion | |
|---|---|
| `smarthome_ui.task_done` / `task_reopen` | Aufgabe (Name wie im Dashboard, z. B. „Bad putzen“) erledigt / wieder offen |
| `smarthome_ui.waste_ack` / `waste_skip` | Müll am Datum rausgestellt / diesmal nicht |
| `smarthome_ui.area_mowed` | Mähfläche gerade gemäht |

```yaml
# Beispiel: Erinnerung, wenn das Bad seit 2 Tagen überfällig ist
triggers:
  - trigger: template
    value_template: "{{ state_attr('sensor.household_bad_putzen', 'days_left') | int(0) <= -2 }}"
actions:
  - action: notify.mobile_app_handy
    data:
      message: "Das Bad ist überfällig"
```

Wird eine Aufgabe hier erledigt (To-do, Aktion), ist sie sofort im Dashboard abgehakt – und umgekehrt.

## Bildschirme

Jedes Gerät, auf dem das Dashboard offen ist (Wandtablet, Handy, Browser), erscheint automatisch als
eigenes Gerät, z. B. „iPad (Paul)“. Umbenennen geht in Home Assistant am Gerät. Ein Bildschirm,
der nicht offen ist, lässt sich dort löschen – öffnet man das Dashboard wieder, kommt er zurück.

| Entität | |
|---|---|
| `binary_sensor.display_<name>_online` | Dashboard ist offen und verbunden (Attribut `last_seen`) |
| `select.display_<name>_page` | aktuelle Seite – auswählen springt dorthin |
| `select.display_<name>_theme` | `dark`, `sand`, `clay`, `auto` |
| `select.display_<name>_season` | Jahreszeiten-Deko: `auto`, `off` oder eine feste (z. B. `herbst`, `weihnachten`) |
| `select.display_<name>_sounds` | Töne: `off`, `quiet`, `normal` |
| `select.display_<name>_performance` | Leistung: `auto`, `full`, `lite` |
| `switch.display_<name>_standby` | Ruhemodus (Uhr, Wetter) – an zeigt ihn sofort, ein Tipp am Gerät beendet ihn |
| `sensor.display_<name>_user` | wer angemeldet ist |
| `button.display_<name>_reload` | Dashboard neu laden |

Die Einstellungen gelten je Gerät – wie im Dashboard selbst. Meldet sich jemand anderes am Gerät an,
bleibt es derselbe Bildschirm; `sensor.display_<name>_user` zeigt, wer gerade angemeldet ist.

```yaml
# Beispiel: bei Klingeln das Flur-Tablet aufwecken und die Sicherheitsseite zeigen
triggers:
  - trigger: state
    entity_id: event.haustur_klingel
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.display_wandtablet_flur_standby
  - action: select.select_option
    target:
      entity_id: select.display_wandtablet_flur_page
    data:
      option: Sicherheit
```

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
| `smarthome_ui/display/connect` | offenes Dashboard als Bildschirm anmelden; Befehle (`set`, `reload`, `reconnect`) kommen als Ereignisse |
| `smarthome_ui/display/update` | Zustand eines Bildschirms (Seite, Theme, Jahreszeit, Töne, Leistung, Ruhemodus, Benutzer) |

## Entwicklung

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```
