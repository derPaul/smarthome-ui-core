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

## Mähroboter

Unter *Einstellungen → Geräte & Dienste → Smarthome UI Core* gibt es **„Mähroboter hinzufügen“** – beliebig
viele. Gewählt wird nur das Gerät (alles mit einer `lawn_mower`-Entität), optional ein eigener Name. Den Rest
findet Core am Gerät selbst (z. B. bei Mammotion, Husqvarna, Gardena):

- **Mähflächen:** aus den Bereichs-Schaltern des Mähers und dem, was er mäht (Flächen-Sensor während einer
  Aufgabe oder Positions-Sensor). Core merkt sich je Fläche, wann sie zuletzt gemäht wurde, wie lange und wie
  (z. B. „Kanten“) – **auch ohne offenes Dashboard**. Eine kurze Durchfahrt (unter 10 Minuten, nur laut Position)
  zählt nicht.
- **Entitäten am Mäher-Gerät:** je Fläche `sensor.garden_<fläche>_last_mowed` (Zeitstempel, Attribute `mode`,
  `minutes`, `mowing`). Sie erscheinen auf der Geräteseite des Mähers.
- **Mitteilungen** (Quelle „Mähroboter“ im Dashboard, Push an): Fehler, Selbsttest meldet ein Problem
  (länger als 2 Minuten), Messer wechseln.

**Mähplan:** je Mäher, wie viele Tage zwischen zwei Mähgängen liegen (je Monat; Vorschlag für Mitteleuropa,
November bis Februar Pause) oder ein festes Intervall – und je Fläche öfter / normal / seltener / Pause.
Bearbeitet wird im Mähplan-Popup des Dashboards; die Häufigkeit je Fläche gibt es auch als
`select.garden_<fläche>_frequency`. Daraus rechnet Core:

- am Sensor `sensor.garden_<fläche>_last_mowed` die Attribute `interval_days`, `next_due`, `days_left`, `due`,
- fällige Flächen in `todo.garden` („Vorgarten mähen“ – abhaken = von Hand gemäht),
- eine Mitteilung am Fälligkeitstag zur Uhrzeit für fällige Aufgaben (Quelle „Mähroboter“, Push an).

Das Dashboard nimmt die Mähroboter dann aus Core – bei mehreren mit Umschalter in der Kachel. Dort lässt sich nur
noch einstellen, welche Entitäten die Kachel zeigt und wie sie heißen.

## Saugroboter

**„Saugroboter hinzufügen“** funktioniert genauso (beliebig viele, Gerät wählen). Core findet am Gerät – und an einer
damit verbundenen Station – den Sauger, den Sensor „Aktueller Raum“ (z. B. Roborock), Fehler-Sensoren, die
gereinigte Fläche und die Verschleißteile.

- **Räume:** Solange der Sauger reinigt, zählt Core die Zeit je Raum mit; ab 2 Minuten gilt ein Raum als gesaugt.
  Stopps an der Station bis 45 Minuten (Mopp waschen, laden) gehören zum selben Lauf. Ohne Raum-Sensor zählt ein
  im Dashboard gestarteter Lauf für seine Räume, eine Komplettreinigung für alle zugeordneten Räume.
  Raumnamen werden über „Staubsauger-Segmente Bereichen zuordnen“ bzw. den Namen den Bereichen zugeordnet.
- **Entitäten am Sauger-Gerät:** je Raum `sensor.household_<raum>_last_vacuumed`.
- **Mitteilungen** (Quelle „Saugroboter“, Push an): Fehler, Roboter- und Stationsfehler (z. B. Wasser, Beutel),
  Verschleißteile unter 10 % (Haupt-/Seitenbürste, Filter, Sieb, Sensoren).
- **Putz-Intervalle je Raum** (gilt für Roboter und von Hand): `number.household_<raum>_clean_interval`
  (Tage, 0 = nicht verfolgen) am Gerät „Haushalt“; im Dashboard wie bisher über den Raum änderbar.

Das Dashboard nimmt die Saugroboter dann aus Core (bei mehreren mit Umschalter); die Kachel zeigt nur noch
Sichtbarkeit und Namen der Entitäten.

## Automatisch mähen und saugen (Roboter-Planer)

Je Mäh- und Saugroboter gibt es einen Planer, der den Roboter **von selbst startet, wenn Flächen bzw. Räume fällig
sind**. Er ist zunächst aus.

| Entität (am Roboter-Gerät) | |
|---|---|
| `switch.garden_<mäher>_auto` / `switch.household_<sauger>_auto` | Automatisch mähen / saugen – an oder aus |
| `switch.garden_<fläche>_auto` / `switch.household_<raum>_auto` | je Fläche bzw. Raum: darf automatisch dran sein |
| `sensor.garden_<mäher>_planner` / `sensor.household_<sauger>_planner` | worauf der Planer wartet (Zeitfenster, Ruhezeit, jemand zuhause, Regen, Solarstrom, Roboter nicht bereit), „Startet gleich“, „Gestartet“ – Attribute `due` (fällige Flächen/Räume) und `start_at` |
| `button.garden_<mäher>_skip_today` / `button.household_<sauger>_skip_today` | heute nicht |

Bedingungen – unter *Smarthome UI Core → Mähroboter/Saugroboter → Zahnrad (ändern)*, jede abschaltbar:

- **Zeitfenster** (Mäher 10–19 Uhr, Sauger 9–18 Uhr),
- **nicht in der Ruhezeit** der Mitteilungen,
- **nur wenn niemand zuhause ist** (`person.*`; beim Sauger an, beim Mäher aus),
- **nicht bei Regen** (Mäher: Wetter und Regensensor des Mähers),
- **nur mit Solarstrom**: ein Sensor mit PV-Leistung oder Überschuss (W/kW) und ein Mindestwert,
- **vorher ankündigen** (Standard 10 Minuten): Push „Robbi saugt ab 10:30: Küche, Flur“ mit dem Knopf
  **„Heute nicht“** (Quelle „Saugroboter“ bzw. „Mähroboter“ muss Push an haben). 0 = sofort starten.

**Wischen** (Saugroboter mit Wischfunktion): Core erkennt sie am Reinigungsmodus (z. B. „Saugen“ /
„Saugen und Wischen“) oder an einer Wasser-/Wischstufe mit „Aus“ (z. B. Roborock). Dann plant der Sauger getrennt:

- **Nur saugen** – so oft wie das Putz-Intervall des Raums (Wischen dabei aus),
- **Wischen** – alle *N* Tage (Einstellung „Wischen alle … Tage“, Standard 7, 0 = Planer wischt nicht):
  erst wird gesaugt, danach – sobald der Sauger zurück ist – wird im zweiten Durchgang gewischt.
- je Raum `switch.household_<raum>_mop` (z. B. für Teppichräume aus).
- Gewischte Räume merkt sich Core (Attribut `last_mopped` am Raum-Sensor), auch bei Läufen aus der App.

**Etagen:** Der Sauger startet nur Räume auf seiner Etage. Wo er steht, erkennt Core an einer Karten-Auswahl, deren
Name zu einer Etage passt (Roborock: eine Karte je Etage, z. B. „Obergeschoss“), sonst am Bereich des Saugers und
dessen Etage. Sind Räume auf einer anderen Etage fällig, kommt einmal am Tag eine Mitteilung („Robbi steht im
Erdgeschoss – fällig sind Obergeschoss: Bad. Roboter umsetzen?“); Attribut `unreachable` am Planer-Sensor.
Räume ohne Etage gelten als erreichbar.

**Übliche Dauer:** Core merkt sich je Fläche bzw. Raum die letzten zehn Laufzeiten (beim Sauger Saugen und Wischen
getrennt). Ab drei Läufen gibt es eine Warnung, wenn ein Lauf weniger als halb oder fast doppelt so lange dauert
wie üblich (z. B. „Luba: Vorgarten nur 12 min statt üblich 40 min – nicht fertig geworden?“). Attribute
`usual_minutes` (und `last_minutes`) an den Flächen- und Raum-Sensoren.

Gestartet wird nur mit den fälligen Flächen/Räumen – Sauger über `vacuum.clean_area` (Räume aus „Staubsauger-Segmente
Bereichen zuordnen“, sonst `vacuum.start`), Mäher über seine Bereichs-Schalter und `lawn_mower.start_mowing` (sonst
eine Aufgabe mit dem Namen der Fläche). Höchstens ein automatischer Start je Roboter und Tag. Fällig heißt: Mähplan
(Mäher) bzw. Putz-Intervall des Raums (Sauger, wie im Dashboard).

## Saugen, wenn alle weg sind

Core bringt die Aktion **`smarthome_ui.vacuum_rooms`** mit: saugt die gewählten Bereiche jetzt – nur Räume auf der Etage
des Saugers, auf Wunsch nur saugen (Wischen aus) und ohne Bereiche, die in den letzten *N* Stunden gesaugt wurden. Läuft
der Sauger gerade oder hat er einen Fehler, passiert nichts. Antwort: `{"started": [...]}`.

Dazu legt Core eine **Vorlage (Blueprint)** an: *Einstellungen → Automationen & Szenen → Blueprints →
„Bereiche saugen, wenn alle weg sind“*. Eingaben: Saugroboter, Bereiche (z. B. Eingangsbereich), Wartezeit nach dem
Gehen (Standard 5 min), Auslassen, wenn kürzlich gesaugt (4 h), Zeitfenster (7–21 Uhr). Ausgelöst wird, wenn
`zone.home` auf 0 fällt – also wenn der Letzte das Haus verlässt. Im Dashboard lässt sich die Automation in der
Saugroboter-Kachel anlegen, ein- und ausschalten.

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
| `smarthome_ui/get` | Daten (`done`, `done_log`, `household`, `mow_log`), Mäh- und Saugroboter, Putz-Intervalle, Status |
| `smarthome_ui/subscribe` | Ereignis bei jeder Datenänderung |
| `smarthome_ui/import` | einmalige Datenübernahme aus dem Dashboard |
| `smarthome_ui/done` | Abhaken / wieder öffnen / Rückgängig (`changes`, `log`, `unlog`) |
| `smarthome_ui/data/set` | `household` bzw. `mow_log` ersetzen |
| `smarthome_ui/tasks/sync` | Aufgaben mit Fälligkeit |
| `smarthome_ui/notify/config` | Regeln, Empfänger, Quellen, Ruhezeiten, Uhrzeit für Aufgaben |
| `smarthome_ui/notify/report` | aktuelle Meldungen eines offenen Dashboards |
| `smarthome_ui/mower/settings` | Mähplan eines Mähers (`plan` je Monat, `interval`, `freq` je Fläche) |
| `smarthome_ui/cleaning/settings` | eigene Putz-Intervalle je Raum (`intervals`) |
| `smarthome_ui/display/connect` | offenes Dashboard als Bildschirm anmelden; Befehle (`set`, `reload`, `reconnect`) kommen als Ereignisse |
| `smarthome_ui/display/update` | Zustand eines Bildschirms (Seite, Theme, Jahreszeit, Töne, Leistung, Ruhemodus, Benutzer) |

## Entwicklung

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```
