# Smarthome UI Core

Begleit-Integration zum [Smarthome-UI-Dashboard](https://github.com/derPaul/smarthome-ui). Sie
übernimmt alles, was **laufen soll, während niemand aufs Dashboard schaut**:

- **Mitteilungen** – Push-Nachrichten kommen aus Home Assistant, nicht mehr von einem offenen
  Dashboard („Sender-Gerät“ entfällt).
- **Haushalt & Garten** – Putzplan, Wartung, Gartenkalender, Müll und Mähbereiche liegen hier statt
  im Frontend-Speicher, als Entitäten nutzbar (Automationen, Sprache, andere Dashboards).

Eingestellt wird alles im Dashboard. Ab 0.31 bringt Core auch **das Dashboard selbst** mit: Es lädt die Karte aus
GitHub und bindet sie in Home Assistant ein (siehe [Dashboard](#dashboard)).

## Installation

1. In HACS als benutzerdefiniertes Repository (Typ *Integration*) hinzufügen und installieren –
   oder `custom_components/smarthome_ui` nach `/config/custom_components/` kopieren.
2. Home Assistant neu starten.
3. *Einstellungen → Geräte & Dienste → Integration hinzufügen → Smarthome UI Core*.
4. Das Dashboard-Repository ist privat: GitHub-Token (fine-grained, nur *Contents: Read-only* für
   `derPaul/smarthome-ui`) in `/config/.smarthome_ui_token` ablegen oder unter *Smarthome UI Core → Konfigurieren*
   eintragen, dort auch den Branch (Standard `main`). Core lädt die Karte eine Minute nach dem Start.
5. Seite neu laden, ein Dashboard anlegen und die Karte `custom:smarthome-dashboard-card` einfügen – oder den
   Einrichtungsassistenten das Dashboard anlegen lassen. Er ordnet auch die Geräte zu. Fertig.

HACS lädt die Dateien direkt aus dem Repository (ab 0.30). Die Release-ZIP `smarthome_ui.zip` bleibt für die
Installation von Hand; über `github.com/…/releases/download/` scheitert der Download in Home Assistant derzeit an
einem zu langen Header von GitHub („Got more than 8190 bytes“).

Von einer Dashboard-Version vor 1.279 erst auf 1.279 aktualisieren und einmal öffnen – ab 0.30 nimmt Core keine
Daten mehr aus dem Dashboard entgegen.

## Dashboard

Ab 0.31 lädt Core die Dashboard-Karte (`dist/smarthome-dashboard-card.js`) aus dem privaten Repository
`derPaul/smarthome-ui`, legt sie unter `/config/smarthome_ui/` ab und lädt sie auf jeder Seite von Home Assistant
(`/smarthome_ui_ui/smarthome-dashboard-card.js?v=<Version>` – die Version in der Adresse sorgt dafür, dass Browser
und App nach einem Update die neue Karte nehmen). Eine Dashboard-Ressource, `shell_command` oder Einträge in der
`configuration.yaml` braucht es dafür nicht mehr.

- **Erste Version:** installiert Core selbst, eine Minute nach dem Start.
- **Neue Versionen** prüft Core alle sechs Stunden. Sie erscheinen als Update **Smarthome UI**
  (`update.system_dashboard`) unter *Einstellungen → Updates*, mit den Release Notes aus dem CHANGELOG. Installieren
  dort oder im Dashboard (*Einstellungen → Update*), danach die Seite neu laden. Ein Neustart ist nicht nötig.
- **Branch und Token** unter *Smarthome UI Core → Konfigurieren*. Ohne eigenen Token nimmt Core
  `/config/.smarthome_ui_token` (derselbe wie für das alte Update-Skript).
- **Dashboard-YAML des Update-Skripts** (`/config/dashboards/smarthome.yaml`, ab 0.34): Im Dashboard unter
  *Menü → Darstellung → Seiten hinzufügen* holt Core die Datei neu aus GitHub (`examples/dashboard-komplett.yaml`),
  so kommen neue Seiten wie „Musik“ dazu. Die alte Datei bleibt als `smarthome.yaml.bak`. Andere YAML-Dateien
  ändert Core nicht.
- **Umstieg vom Update-Skript:** Läuft die Karte zusätzlich noch über die alte Ressource (`/local/smarthome-loader.js`
  bzw. `/local/smarthome-dashboard-card.js`), zeigt das Dashboard einen Hinweis. Dann die Ressource unter
  *Einstellungen → Dashboards → ⋮ → Ressourcen* löschen und die beiden `shell_command`-Einträge `smarthome_ui_update`
  und `smarthome_ui_check` aus der `configuration.yaml` entfernen. Ein Dashboard im YAML-Modus
  (`dashboards/smarthome.yaml`) bekommt dann keine neuen Seiten mehr; der Einrichtungsassistent legt ein
  gleichwertiges Dashboard in Home Assistant an.

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
- **Einzelne Meldungen stumm** – im Dashboard (Mitteilungen → Einzelne Meldungen) lässt sich genau eine
  Meldung auf „Kein Push“ oder „Aus“ stellen, z. B. eine, die jeden Abend kommt. Zahlen und Uhrzeiten im Text
  zählen dabei nicht; Test-Mitteilungen kommen immer.
- **Empfänger** – die im Dashboard gewählten `notify`-Dienste, sonst alle `notify.mobile_app_…`.
- **Verlauf** (ab 0.55) – jede Mitteilung der letzten 30 Tage, auch die nicht verschickten (stumm, pausiert,
  im Urlaub zurückgehalten, in der Ruhezeit verschoben), mit ihren Aktionen und der am Handy gewählten
  Antwort. Im Dashboard unter Mitteilungen → Verlauf; eine Aktion (z. B. „Wasser absperren“) lässt sich dort
  nachträglich auslösen. Gespeichert in einer eigenen Datei (`.storage/smarthome_ui_core.notify_log`,
  höchstens 1500 Einträge).

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

## Aufgaben, Müllabfuhr und Wächter

**Einkauf:** Den Wert des Sensors „Ausgaben letzter Monat“ (z. B. aus einer Kassenbon-Integration) merkt sich Core
je Monat – so hat das Dashboard die Monatsübersicht auch ohne offenes Dashboard am Monatswechsel.

**Aufgaben:** Welche Aufgaben es gibt (Wartung, Gartenkalender, Putz-Räume) und wie oft sie dran sind, legt das
Dashboard fest – es schickt die Definitionen nach jeder Änderung an Core. **Fällig rechnet Core selbst** (Intervall
oder feste Monate, Erledigt-Einträge, Saugroboter-Zeiten), also auch ohne offenes Dashboard. Die früheren Helfer
„Wartung …“ und die Automation „Smarthome: Wartung fällig“ braucht es damit nicht mehr; das Dashboard bietet in der
Wartungs-Kachel „Alte Push-Helfer entfernen“ an.

**Müllabfuhr:** Core liest die Abfallkalender selbst (gewählte oder alle, die nach Müll klingen) und erkennt die
Sorten (Restmüll, Bio, Papier, Gelbe Tonne, Grünschnitt …):

| Entität | |
|---|---|
| `sensor.household_waste_next` | nächste Abholung (Datum), Attribute `kinds`, `days_until`, `reminder`, `put_out`, `upcoming` |
| `sensor.household_waste_<sorte>` | nächste Abholung je Sorte, z. B. `sensor.household_waste_biotonne` |
| `button.household_waste_put_out` | rausgestellt |

Zur Erinnerungszeit (Standard: Vortag 12 Uhr) steht „Morgen: Restmüll rausstellen“ in `todo.household` und kommt als
Mitteilung (Quelle „Müllabfuhr“) – bis „rausgestellt“ (Knopf, To-do oder Dashboard).

**Wächter** (werden zu Aufgaben in den To-do-Listen; abhaken blendet sie aus, bis das Problem wieder auftritt):

- **Batterien** unter 15 % (Handys ausgenommen) → „Batterie wechseln: Fenster Bad (9 %)“ (Quelle „Batterien“),
- **Pflanzen** – siehe [Pflanzen](#pflanzen),
- **Frost**: die stündliche Vorhersage geht in den nächsten 18 Stunden auf 1 °C oder darunter → „Pflanzen vor Frost
  schützen“ und am späten Nachmittag eine Mitteilung „Frost heute Nacht (−2 °C)“ (Quelle „Frostwarnung“).

**Saugroboter-Station:** Wassermangel, Schmutzwasser voll, Staubbeutel und weitere Problem-Sensoren der Station
werden als Mitteilung gemeldet (Quelle „Saugroboter“).

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
  zählt nicht. **Laub sammeln** (Mäher mit Sammelbehälter wie Yuka: Schalter „Sammeln“/„Entleeren“ an, bis
  15 Minuten danach) zählt ebenfalls nicht als Mähen.
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

**Mittagsruhe** (ab 0.47): je Mäher ein Zeitfenster (z. B. 13:00–15:00), an Wochenenden und Feiertagen
(Feiertage über den Arbeitstag-Sensor der Integration *Workday*, ohne ihn nur Sa/So), nur am Wochenende oder
täglich – und für welche Modi (Mähen, Grasschnitt sammeln, Laub sammeln, Kanten). Ist der Mäher zu Beginn in einem
dieser Modi unterwegs, schickt Core ihn in die Ladestation (`lawn_mower.dock`); nach dem Fenster setzt er die Aufgabe
fort (Taste „Letzte Aufgabe fortsetzen“, sonst `lawn_mower.start_mowing`). Startet man ihn in der Pause von Hand,
bleibt er draußen. Der Planer startet in der Mittagsruhe nicht (Status „Mittagsruhe“). Eingestellt wird im
Mähplan-Popup des Dashboards (Websocket `smarthome_ui/mower/settings` mit `noon`).

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
- **Mitteilungen** (Quelle „Saugroboter“, Push an): Fehler, Roboter- und Stationsfehler, Station: Frischwasser
  nachfüllen, Schmutzwasser leeren, Staubbeutel wechseln (Binärsensoren und Tank-Sensoren mit Zustand),
  Verschleißteile unter 10 % (Haupt-/Seitenbürste, Filter, Sieb, Sensoren).
- **Putz-Intervalle je Raum** (gilt für Roboter und von Hand): `number.household_<raum>_clean_interval`
  (Tage, 0 = nicht verfolgen) am Gerät „Haushalt“; im Dashboard wie bisher über den Raum änderbar.

Das Dashboard nimmt die Saugroboter dann aus Core (bei mehreren mit Umschalter); die Kachel zeigt nur noch
Sichtbarkeit und Namen der Entitäten.

## Pflanzen

Für jede Pflanze (`plant.*`, z. B. aus der Integration Plant Monitor) hängt Core eigene Entitäten an das Gerät der
Pflanze:

| Entität | |
|---|---|
| `sensor.<garden\|household>_<pflanze>_last_watered` | zuletzt gegossen (Attribut `by`: `auto` oder `hand`) |
| `sensor.<garden\|household>_<pflanze>_next_watering` | wann die Erde den unteren Grenzwert erreicht (Attribute `drying_per_day`, `min_moisture`) |
| `button.<garden\|household>_<pflanze>_watered` | „Gegossen“ |

- **Gießen erkennen:** Steigt die Bodenfeuchte innerhalb einer Stunde um 10 Punkte, gilt die Pflanze als gegossen.
  Ohne Sensor oder zusätzlich: Knopf „Gegossen“ oder die Aufgabe abhaken.
- **Vorschau:** Aus dem Abtrocknen seit dem letzten Gießen (mindestens 12 Stunden Werte) schätzt Core, wann der untere
  Grenzwert erreicht ist (Grenzwert von Plant Monitor, sonst 20 %). „<Pflanze> gießen“ steht dann an diesem Tag in der
  To-do-Liste und kommt zur Aufgaben-Uhrzeit als Mitteilung – ebenso, wenn Plant Monitor „Feuchte niedrig“ meldet.
- **Richtige Liste:** Pflanzen in einem Bereich wie Garten, Balkon oder Terrasse (und ohne Bereich) → Liste „Garten“,
  Quelle „Pflanzen“; alle anderen → Liste „Haushalt“, Quelle „Zimmerpflanzen“.
- **Regen:** Draußen entfällt das Gießen, wenn es in den letzten 12 Stunden geregnet hat (Wetter, Regensensor,
  Niederschlagssensor) oder in den nächsten 6 Stunden Regen kommt (stündliche Vorhersage).
- **Sensor ausgefallen:** Liefert der Feuchtesensor 24 Stunden nichts, kommt „Sensor <Pflanze> prüfen“.
- **Düngen:** mit Dünger-Sensor wie bisher, wenn er „niedrig“ meldet. Ohne Sensor nach Kalender: alle *N* Wochen
  zwischen zwei Monaten (im Dashboard an der Pflanzen-Kachel einstellbar, WebSocket `smarthome_ui/plants/settings`).

## Alarmanlage

Core führt die Alarm-Logik selbst aus – statt der Automationen, die das Dashboard früher geschrieben hat („Alarm –
Auslöser“, „Alarm – Reaktion“, „Gäste“, „Nachtweg“, „Nachtweg – Rückfrage“) und seiner Helfer „Gäste da“ und
„Nachtweg bis“. Die Alarmzentrale selbst (Manual, Alarmo …) bleibt; Core löst sie aus und reagiert auf sie.
Eingestellt wird weiter im Dashboard (*Sicherheit → Alarmanlage → Zahnrad*); die Einstellungen liegen in Core.

- **Auslöser:** je Sensor die Modi, in denen er den Alarm auslöst (Zuhause, Abwesend, Nacht, Urlaub).
- **Reaktion:** Push an die gewählten Handys (auf Wunsch kritisch), Vorwarnung bei der Eingangsverzögerung, Entwarnung
  beim Entschärfen, Sirenen, alle Lichter an, Meldung in Home Assistant mit dem auslösenden Sensor.
- **Gäste:** Gäste-Geräte, WLANs oder das UniFi-Gästenetz schalten „Gäste da“ ein (aus, wenn alle eine Weile weg
  sind). Dann sind die gewählten Bewegungsmelder in den Gäste-Modi stumm; auf Wunsch Push beim Scharfschalten.
  Von Hand eingeschaltet (`switch.security_guests`) bleibt „Gäste da“ an, bis es von Hand ausgeht – die Erkennung
  beendet es dann nicht (ab 0.29.1). Zählt ein bei Gästen stummer Sensor trotzdem, steht der Grund im Logbuch
  („Gäste nicht da“ bzw. „Modus … zählt nicht für Gäste“).
- **Nachtweg:** Bewegung oben öffnet ein kurzes Fenster, in dem Bewegung unten nicht als Einbruch zählt – nur wenn
  unten vorher alles ruhig war. Auf Wunsch fragt Core per Push „bist du das?“ und löst ohne Antwort aus.

| Entität (Gerät „Sicherheit“) | |
|---|---|
| `switch.security_alarm_logic` | Alarm-Logik in Core an/aus. Aus: die alten Dashboard-Automationen werden wieder eingeschaltet (sie bleiben erhalten), an: ausgeschaltet – nie beide, nie keine |
| `switch.security_guests` | Gäste da (Attribut `devices`) – auch von Hand schaltbar |
| `sensor.security_nightwalk_until` | bis wann der Nachtweg offen ist |
| `button.security_nightwalk_open` | Nachtweg jetzt öffnen |
| `sensor.security_alarm_reason` | welcher Sensor zuletzt ausgelöst hat |

Einstellungen als Konfigurations-Entitäten (gleicher Stand wie im Dashboard-Dialog, erst nach der Übergabe
verfügbar). Die Listen – welche Sensoren, welche Modi – bleiben im Dialog.

| Einstellung | |
|---|---|
| `switch.security_nightwalk` | Nachtweg an/aus |
| `select.security_nightwalk_effect` | Bewegung unten im Nachtweg: per Push nachfragen (`ask`) oder still erlauben (`allow`) |
| `number.security_nightwalk_window` | wie lange der Nachtweg offen ist (1–30 Min.) |
| `number.security_nightwalk_quiet` | so lange muss unten vorher Ruhe sein (0–60 Min.) |
| `number.security_nightwalk_ask_delay` | Alarm, wenn die Rückfrage so lange unbeantwortet bleibt (10–300 s) |
| `number.security_guests_away` | „Gäste da“ geht so lange nach dem letzten Gast aus (5–240 Min.) |
| `switch.security_alarm_critical` | Alarm-Push als kritische Mitteilung |
| `switch.security_alarm_pending` | Push bei Eingangsverzögerung |
| `switch.security_alarm_clear` | Push, wenn der Alarm beendet ist |
| `switch.security_alarm_lights` | bei Alarm alle Lichter an |
| `switch.security_guests_push` | Push beim Scharfschalten mit Gästen |

Bei der Übergabe (Dashboard: „An Core übergeben“ bzw. Speichern) übernimmt Core auch den aktuellen Stand von
„Gäste da“ und „Nachtweg bis“ und schaltet die alten Automationen aus.


**Warum hat es ausgelöst?** Core schreibt ins Logbuch („Alarmanlage“), welcher Sensor ausgelöst hat. Sind Gäste da und der Sensor ist nicht „bei Gästen stumm“ (Personen-Symbol im Reiter Auslöser), steht dort ab 0.33.1 „Gäste da, aber … ist bei Gästen nicht stumm geschaltet“. Die Diagnose enthält dazu die Regeln, die Gäste-Einstellungen und den Nachtweg (`alarm_settings`).
## Klingel

**„Klingel hinzufügen“** (beliebig viele): Gerät der Klingel wählen (UniFi Protect, Reolink, Ring …), optional Name,
Kamera und Schloss. Core findet am Gerät das Klingel-Ereignis (bzw. den Klingel-Sensor) und die Kamera (Hauptkanal,
nicht die Paket-Kamera), das Schloss im selben Bereich.

- Klingelt es, kommt „Es klingelt: Haustür“ mit dem Kamerabild (Quelle „Klingel“, Push an) – dringlich, wenn niemand
  zuhause ist. Das Dashboard schickt für diese Klingeln keine eigene Regel mehr.
- Entitäten am Klingel-Gerät: `sensor.security_<klingel>_last_ring` (zuletzt geklingelt, Attribut `today`) und
  `switch.security_<klingel>_notify` („Klingel-Mitteilung“ – aus, z. B. wenn das Baby schläft).
- Die Klingel-Kachel im Dashboard nimmt die Klingeln dann aus Core.

## Sicherheit

Gerät **„Sicherheit“** mit Prüfungen für offene Fenster und Türen und einer Übersicht der letzten Nacht. Mitteilungen
kommen über die Quellen „Fenster & Türen“ bzw. „Letzte Nacht“ (Push an).

| Entität | |
|---|---|
| `sensor.security_open` | Anzahl offener Fenster & Türen (Attribute `open`, `unlocked`, `persons_home`) |
| `switch.security_rain_warning` | Regenwarnung: offen und es regnet oder Regen kommt in den nächsten 2 Stunden |
| `switch.security_leave_check` | wenn der Letzte das Haus verlässt: was noch offen bzw. nicht abgeschlossen ist |
| `switch.security_open_long_check` / `number.security_open_minutes` | Fenster oder Tür seit 10 Minuten offen (Minuten einstellbar): „Bad ist seit 10 Minuten offen“ |
| `switch.security_presence_sim` | Anwesenheit simulieren (siehe unten) – Attribut `state`: aus, wartet, bereit, läuft |
| `switch.security_good_night_check` / `time.security_good_night` | Gute-Nacht-Check zur eingestellten Uhrzeit (Standard aus, 22:30): offen, nicht abgeschlossen, Alarm nicht scharf |
| `sensor.security_persons_home` | Personen zuhause (Attribute `home`, `asleep`) |
| `binary_sensor.security_<person>_asleep` | Person schläft: zuhause, Handy lädt seit 10 Minuten, zwischen 21:30 und 9 Uhr |
| `binary_sensor.security_all_asleep` | jemand ist zuhause und alle zuhause schlafen – z. B. für „Alarm auf Nacht“ |
| `sensor.security_last_night` | erkannte Personen der letzten Nacht; Attribute: Anzahl je Art (Personen, Tiere, Fahrzeuge, Bewegung im Haus, Türen) und die Ereignisse |

- **Fenster & Türen:** Fenster-, Tür- und Garagentor-Sensoren – ohne Gerätetüren (Kühlschrank, Spülmaschine …,
  Briefkasten). Was das Dashboard als Fenster & Türen eingestellt hat, gilt vorrangig.
- **Klingel mit Bild:** Kommt die Klingel-Mitteilung vom Dashboard mit einer Kamera, hängt Core das Standbild an
  (`image: /api/camera_proxy/camera.…`); auf dem iPhone zeigt die geöffnete Mitteilung das Livebild.
- **Letzte Nacht** (Standard 22:00–06:00, Zeiten und Sensoren wie im Dashboard eingestellt): Core zählt live mit –
  Personen- und Tier-Erkennungen (z. B. Kameras), Fahrzeuge (nur wenn gewählt), Bewegung im Haus (ohne Kameras und
  Außenbereiche) und geöffnete Türen. Am Ende der Nacht kommt eine Zusammenfassung, wenn eine Person erkannt oder eine
  Tür geöffnet wurde, z. B. „1 Person erkannt (Einfahrt 02:14) · 1× Tür geöffnet (Haustür 02:14)“.
  Löst die Alarmanlage in der Nacht aus, zählt das mit (mit Auslöser) und steht in der Morgen-Mitteilung an
  erster Stelle – die kommt dann auch, wenn sonst nichts los war (ab 0.29.2).

- **Anwesenheit simulieren:** ersetzt die frühere Automation des Dashboards („Smarthome: Anwesenheit simulieren“).
  Eingestellt wird im Dashboard (Sicherheit → Anwesenheit simulieren): Lichter, Rollos, Beginn (Sonnenuntergang
  oder Uhrzeit), Ende, wie lange ein Licht an bleibt, wie viele gleichzeitig und wann (niemand zuhause, Alarm scharf
  oder immer). Alle 5 Minuten schaltet Core im Abendfenster zufällig ein Licht für eine zufällige Dauer ein; zum
  Ende gehen seine Lichter aus. Rollos schließen kurz nach Sonnenuntergang und öffnen morgens, jeweils zufällig
  verzögert. Beim Übernehmen schaltet Core die alte Automation aus.

### Urlaubsmodus (ab 0.53)

`switch.security_vacation` bzw. im Dashboard „Ich gehe – Check“ → „Länger weg? Urlaubsmodus“. Beim Einschalten:

- Heizung absenken: Thermostate im Heizbetrieb und der Tagbetrieb-Sollwert der Wärmepumpe (Heizkreis) um 3 Grad
  (einstellbar 0–10), Warmwasser auf 40 °C (oder unverändert). Die alten Werte merkt sich Core.
- Anwesenheit simulieren einschalten (falls eingerichtet) und die Alarmanlage abwesend scharf schalten, wenn sie aus ist.
- Nur Wichtiges melden: Sicherheit, Wasser, Kühlschrank, Frost, Internet und Stromausfall kommen weiter, alles andere
  wird zurückgehalten und gezählt.

Mit Rückkehrdatum stellt Core Heizung und Warmwasser 24 Stunden vorher (einstellbar) zurück und meldet das. Kommt
jemand nach Hause (Person „zuhause“), endet der Modus von selbst: alte Werte zurück, Simulation wie vorher, und eine
Zusammenfassung „Willkommen zurück“ (Tage, Alarm, Klingeln, Post, zurückgehaltene Mitteilungen). Websocket
`smarthome_ui/vacation/set` (`on`, `settings`: `offset`, `hot_water`, `sim`, `alarm`, `quiet`, `until` in ms,
`preheat_h`).

## Neue Geräte (ab 0.55)

Geräte, die nach der Einrichtung von Smarthome UI Core dazukommen (z. B. ein Eve Shutter Switch), stehen im
Dashboard als **neues Gerät** – 14 Tage lang oder bis sie eingerichtet oder übergangen sind. Core schlägt vor:

- **Raum** aus dem Namen (ein Raumname darin) oder vom übergeordneten Gerät,
- **Namen** „<Raum> <Art>“, wenn das Gerät nur einen Modellnamen trägt („Eve Shutter Switch 20EBN9901“ →
  „Arbeitszimmer Rollladen“),
- **Diagnose** einschalten: abgeschaltete Diagnose-Entitäten, die bei Fehlern helfen (Signal, Batterie,
  Neustarts, Betriebszeit, Thread-Rolle …),
- **Geräteklasse**: ein Rollladen ohne Art wird „Rollladen“ (bei „Jalousie“ „Jalousie“), ein Kontakt ohne Art
  „Fenster“ (oder „Tür“),
- **Automationen**: Lichter in die Anwesenheitssimulation (die auch im Urlaubsmodus läuft); Rollläden werden
  von selbst zu Fenstern mit eigenem Morgens/Abends. Geräte von Core selbst (Fenster, Displays) zählen ab
  0.56 nicht als neu.

## Fenster (ab 0.55)

Jedes Fenster ist ein eigenes Gerät („Fenster Arbeitszimmer“) im passenden Bereich. Jeder Rollladen
(`cover` der Art Rollladen/Jalousie/Rollo oder ohne Art; keine Garagentore und Markisen) wird von selbst zu
einem Fenster – auch neu angelernte, z. B. ein weiterer Eve Shutter Switch. Fenster ohne Rollladen lassen
sich im Dashboard anlegen. Je Fenster:

- **Rollladen**, **Kontakt** (optional; hat der Bereich genau einen Fensterkontakt, wird er übernommen),
  **Tür** (Terrassentür: Aussperrschutz), **Himmelsrichtung** (für den Hitzeschutz), **Automatik**,
  **Putzstatus**.

| Entität | |
|---|---|
| `select.window_<id>_orientation` | Himmelsrichtung (Nord … Nordwest) |
| `switch.window_<id>_automatic` | nimmt an den Automatiken teil |
| `sensor.window_<id>_cleaned` | zuletzt geputzt (Attribut `due`: fällig) |
| `button.window_<id>_clean` | geputzt |

Automatiken (im Dashboard einstellbar; morgens, abends und Hitzeschutz sind anfangs aus, damit nichts fährt,
bevor du es willst – bestehende Automationen für die Rollläden laufen weiter):

- **Morgens** auf (werktags 7:00, am Wochenende 8:30), **abends** 15 Minuten nach Sonnenuntergang zu.
- **Hitzeschutz** – scheint die Sonne auf die Fassade (Sonne höchstens 70° neben der Blickrichtung, mindestens
  12° hoch) und ist es draußen mindestens 24 °C: auf 25 %; wieder hoch, wenn die Sonne weg ist – außer jemand
  hat den Rollladen inzwischen bewegt. Von Hand bewegt: 3 Stunden Ruhe für dieses Fenster.
- **Aussperrschutz** – eine offene Tür wird nie zugefahren; das abendliche Schließen folgt, sobald sie zu ist.
- **Winter-Modus** (automatisch unter 8 °C draußen, oder fest an/aus) – genau bei Sonnenuntergang zu,
  morgens frühestens 15 Minuten nach Sonnenaufgang auf, kein Hitzeschutz.
- **Nach Helligkeit** (ab 0.56; statt Uhrzeit/Sonne): ein „ist dunkel“-Sensor, z. B. der einer Kamera
  (`binary_sensor.…_ist_dunkel`, wird von selbst gefunden oder im Dashboard gewählt), entscheidet. Ist es
  10 Minuten dunkel, gehen die Rollläden zu; ist es 10 Minuten hell, gehen sie auf – aber nicht vor der
  Morgen-Uhrzeit (wird es früher hell, um diese Uhrzeit). Flackern und ein Neustart (nicht verfügbar)
  zählen nicht; je Abend und Morgen nur einmal.
- **Mit Versatz** (ab 0.56): morgens und abends fährt ein Rollladen nach dem anderen, Raum für Raum, im
  Abstand von etwa 45 Sekunden (zufällig die Hälfte mehr oder weniger) – als ginge jemand durchs Haus.
- **Umbenannt** (ab 0.56): bekommt ein Rollladen eine neue Entitäts-ID (`cover.eve_shutter_switch_…` →
  `cover.gaestewc_rollladen`), bleibt er dasselbe Fenster mit allen Einstellungen. Fenster, deren Rollladen es
  nicht mehr gibt, verschwinden samt Gerät; ein im Dashboard entferntes Fenster nimmt sein Gerät mit.
- Rollläden gehören nicht mehr in die **Anwesenheitssimulation** (der Assistent für neue Geräte schlägt sie
  dort nicht mehr vor); auch im Urlaub fahren sie im gewohnten Rhythmus. Nur Rollläden, die dort noch
  eingetragen sind, überlässt Core ihr, solange sie läuft.

**Putzen:** geputzt wird je Fenster (Taste `button.window_<id>_clean`), in den Aufgaben steht es je Raum –
„Fenster putzen (3)“ im Arbeitszimmer, fällig mit dem am längsten nicht geputzten Fenster (Standard alle 90
Tage). Die Raum-Aufgabe abhaken zählt für alle Fenster des Raums.

## Briefkasten

Zwei Kontaktsensoren am Briefkasten – an der **Einwurfklappe** und an der **Entnahmetür**. Core erkennt sie am Namen
(„Briefkasten“, „Einwurf“, „Entnahme“ …) oder nimmt die im Dashboard gewählten.

- Klappe geöffnet = Post ist da (Öffnungen innerhalb einer Minute zählen als ein Einwurf).
- Tür geöffnet = geleert – ein kurzes Zucken (unter 2 s) oder ein Wackeln bis 90 s nach einem Einwurf zählt nicht.
  Ohne Tür-Sensor: Knopf „Briefkasten geleert“ (oder „Geleert“ im Dashboard).
- `binary_sensor.household_mail` – Post im Briefkasten (Attribute Einwürfe, letzter Einwurf, zuletzt geleert),
  `button.household_mail_emptied`.
- Mitteilungen (Quelle „Briefkasten“, Push an): „Post ist da“ beim ersten Einwurf nach dem Leeren,
  „Briefkasten: Klappe/Tür steht offen“ nach 3 Minuten.
- Verlauf: Einwürfe und Leerungen bleiben 30 Tage gespeichert (ab 0.51.1, vorher 7). Die Dauer (1–365 Tage) wählst
  du im Dashboard im Briefkasten-Fenster („Verlauf speichern“); Websocket `smarthome_ui/mailbox/settings` mit
  `keep_days`. Der Wochenbericht vergleicht die Post damit auch mit der Vorwoche.

## Geräte zuordnen (Geräte-Katalog)

**Einrichtungsassistent** (ab 0.26, Dashboard 1.275): Beim ersten Start schlägt Core für jede Rolle passende Geräte
vor (gefundene Entitäten am Gerät, bei der Kaffeemaschine zusätzlich der Name). Das Dashboard zeigt die Vorschläge,
legt die Zuordnungen an und erzeugt das Lovelace-Dashboard. Ab 0.28 fügt er auch Mähroboter, Saugroboter,
Haushaltsgeräte und Klingeln hinzu (in einem Aufruf, Einstellungen bleiben auf Standard). Bestehende Installationen
gelten als eingerichtet.

Welches Gerät der Wechselrichter, die Batterie, die Wärmepumpe, der Enthärter, der Internet-Router, das Gateway,
ein Switch oder ein Accesspoint ist, steht in Core – nicht mehr im Dashboard. **„Gerät zuordnen“** (beliebig oft):
Gerät wählen, Rolle wählen, optional Name. Core findet am Gerät (und an Geräten, die darüber verbunden sind) die
Entitäten der Rolle selbst – wie das Dashboard bisher; im zweiten Schritt lässt sich jede einzeln ändern (leer =
automatisch), bei der Batterie zusätzlich die nutzbare Kapazität.

- Geräte der **UniFi-Integration** kommen ohne Zuordnung dazu (Rolle aus dem Modell: Gateway, Switch, Accesspoint),
  solange „UniFi-Geräte automatisch“ an ist.
- Das Dashboard zeigt, was im Katalog steht; die Prüfungen von Haustechnik und System & Netzwerk nutzen denselben
  Katalog.
- **Home-Assistant-Server** (Rolle `server`): CPU, Arbeitsspeicher, Speicherplatz und Temperatur findet Core am
  System-Monitor-Gerät, dazu das Home-Assistant-Update und das letzte Backup – auch ohne Zuordnung.
- **Wetterstation** (Schritt 3): Außentemperatur, Luftfeuchte, Luftdruck, Regen, Wind, Böen, Windrichtung und
  Sonnenstrahlung für Startseite, Raumklima und Garten. Ohne Zuordnung nimmt Core ein Gerät mit Wind- oder
  Regensensor und Temperatur.
- **Außenfühler** (ab 0.29, `catalog.outdoor`): Temperatur, Luftfeuchte und Luftdruck von der Wetterstation, sonst
  Fühler in Außenbereichen (Garten, Terrasse, Balkon) oder mit „außen“ im Namen – für Raumklima und Garten.
- **Kaffeemaschine:** Leistung, Schalter und Energie der Steckdose bzw. des Relais.
- **Fernseher, Zuspieler, Sync-Box** (Kino auf der Startseite): ohne Zuordnung findet Core sie über die Integration
  (LG webOS, Samsung, Sony Bravia, Philips, Android TV Remote; Apple TV, Android TV; Hue Sync Box).

**Listen der Seiten** (Schritt 2): Welche Kameras, Fenster & Türen, Bewegungsmelder, Schlösser, Chimes und Sensoren
für „Letzte Nacht“ die Sicherheits-Seite zeigt, welche Dienste die Status-Seite überwacht und was ignoriert wird,
steht in Core. Das Dashboard bearbeitet sie in seinen Dialogen; ohne eigene Liste erkennt die Seite die Geräte
automatisch. Seit Schritt 3 auch: Szenen, Kalender und Listen der Startseite, ignorierte Entitäten der Räume, Garten & Terrasse sowie Aufgaben und Zonen des Mähers.

**Energie-Dashboard** (ab 0.27): Netzbezug, Einspeisung, PV, Batterie geladen/entladen, Leistungssensoren, Wasser,
Gas und die einzelnen Geräte liest Core aus dem Energie-Dashboard von Home Assistant (und bei jeder Änderung neu). Das
Dashboard nutzt sie für Verlauf, Rekorde, Kosten, Energiefluss und Verbraucher, solange dort nichts Eigenes eingestellt
ist (`get` → `energy`).

Seit 0.27 stehen auch die Listen von Haustechnik und Netzwerk in Core (Statusleiste, Energiezähler, Betriebszustände,
Thermostate, Verbraucher, „WLAN & Schalter“, Geräte), dazu die Hausverbrauchs-Leistung als einzelner Wert.

**Einzelne Entitäten ohne Gerät** stehen ebenfalls in Core und gelten auf allen Seiten: Wetter-Entität,
Musik-Player, Lautstärke sowie Start- und Aus-Szene des Kinos, ab 0.25 auch Klappe und Tür des Briefkastens und
der Müll-Kalender.

## Diagnose

**Einstellungen → Geräte & Dienste → Smarthome UI Core → ⋮ → Diagnose herunterladen** (ab 0.30) liefert eine Datei
mit allem, was Core weiß und warum es gehandelt hat: Geräte-Katalog, Listen, Alarm (inklusive „Gäste von Hand“ und
letztem Auslöser), letzte Nacht, Haustechnik, System, die vom Dashboard übergebenen Einstellungen und die letzten
Mitteilungen. Namen, Personen, Benachrichtigungsziele, WLAN-Namen und IP-Adressen sind unkenntlich gemacht,
Haushaltsdaten (Erledigt, Protokolle) nur als Anzahl. Hilft bei Fragen wie „warum ging der Alarm an?“.

## Haustechnik

Gerät **„Haustechnik“**. Welche Sensoren gemeint sind (Wärmepumpe, Wechselrichter, Heizungsdruck, Enthärter,
Durchfluss, Verdichter, Hausverbrauch), schickt das Dashboard so, wie es sie auf der Haustechnik-Seite gefunden hat
oder eingestellt sind. Core prüft jede Minute selbst – auch ohne offenes Dashboard. Mitteilungen kommen über die
Quellen „Heizung & Wärmepumpe“, „Strom, PV & Batterie“ und „Wasser & Enthärter“ (Push an).

- **Störungen:** Fehlerzustand der Wärmepumpe (dringlich) bzw. des Wechselrichters; Wechselrichter, Batterie oder
  Heizkreis seit 10 Minuten nicht erreichbar.
- **Heizungsdruck:** unter dem Mindestdruck (Standard 1,5 bar) „Heizungswasser nachfüllen“, unter 1 bar dringlich.
  Fällt der tägliche Tiefstwert über drei Wochen um 0,08 bar pro Woche oder mehr: „Heizungsdruck fällt“ (Hinweis auf
  eine undichte Stelle oder das Ausdehnungsgefäß).
- **Enthärter:** Salz bei 20 % oder weniger, Alarme, Warnungen und Hinweise des Entkalkers (z. B. SYR).
- **Wasser-Wächter:** Wasser fließt 40 Minuten ohne Pause (nachts zwischen 1 und 5 Uhr schon nach 10 Minuten,
  dringlich). Gibt es ein Hauptventil (`valve.…` bzw. Schalter mit „Haupt“, „Absperr“, „Leck“ im Namen), hat die
  Mitteilung den Knopf „Wasser absperren“.
- **Takten:** Der Verdichter startet über 6 Stunden mehr als 4-mal pro Stunde.
- **Grundlast:** Der niedrigste Hausverbrauch zwischen 0 und 5 Uhr liegt deutlich über dem üblichen (Mittel der
  letzten Nächte).

| Entität | |
|---|---|
| `sensor.technik_hints` | Anzahl aktueller Hinweise (Attribute `hints`, `pressure_trend`, `valve`) |
| `switch.technik_pressure_watch` / `water_watch` / `cycling_watch` / `baseload_watch` | Prüfungen an/aus |
| `number.technik_pressure_min` | Mindest-Heizungsdruck (bar) – eigener Wert hat Vorrang vor dem Dashboard |
| `number.technik_water_minutes` | nach wie vielen Minuten fließendes Wasser gemeldet wird |

**Prognosen** (für eigene Automationen, gleiche Rechnung wie auf der Haustechnik-Seite):

| Entität | |
|---|---|
| `sensor.technik_battery_empty` | wann die Batterie die Reserve erreicht (leer = nicht in den nächsten 36 Stunden); Attribut `min_soc` |
| `sensor.technik_battery_charge` | ab wann sie wieder lädt |
| `sensor.technik_solar_window` / `binary_sensor.technik_solar_window_now` | die 3 Stunden heute mit dem meisten Sonnenüberschuss laut Solarprognose des Energie-Dashboards (ab 2,5 kWh); Attribute `end`, `kwh` – z. B. Spülmaschine oder Wallbox starten, wenn „Sonnenfenster jetzt“ an geht |
| `sensor.technik_dhw_next` | wann die Wärmepumpe voraussichtlich Warmwasser macht; Attribute `start_temperature` (gelernt), `cooling_rate`, `heating` |

Die Batterie-Prognose lernt den typischen Verlauf je Stunde selbst mit (Ladeleistung jede Minute, 7 Tage) – sie
braucht dafür etwa 3 Tage. Die kommenden Läufe der Wärmepumpe rechnet sie gesondert ein: den typischen Heiztakt (Laufzeit
und Pause des Heizbetriebs, nicht im Sommerbetrieb) und den nächsten Warmwasser-Lauf, jeweils mit der Leistung, die
die Wärmepumpe im Lauf aufnimmt (Attribut `hp_runs` = Läufe bis leer). Die Warmwasser-Temperatur, ab der nachgeheizt wird, lernt Core aus dem Tiefstwert vor
jedem Aufheizen (bis dahin Soll − 5 K).


**Störung der Wärmepumpe** (ab 0.53.1): Neben der eingestellten Fehler-Entität prüft Core auch die
Fehlercode-Sensoren, die das Dashboard an der Wärmepumpe findet („Aktiver Fehler“, „Fehlercode“) – manche
Integrationen melden eine Störung nur dort, während der Fehlerstatus „aus“ bleibt. Steht der Verdichter
seit mindestens einer Stunde, heißt die Mitteilung „Wärmepumpe steht: Fehler … – Verdichter seit über N Stunden
aus – Warmwasser … °C“ (dringend). Was schon gemeldet wurde, merkt sich Core über Neustarts hinweg: dieselbe
Störung kommt nicht erneut, ein neuer Fehlercode schon. Bei Stiebel Eltron und Tecalor erklärt Core den Code aus der
WPM-Meldungsliste des Herstellers (`data/stiebel_wpm.json`, deutscher Teil, Quelle:
[stiebel-eltron.de](https://www.stiebel-eltron.de/content/dam/ste/de/privatkunden/de/downloads/redirects/wpm-meldungsliste.pdf)):
was ausgelöst hat und was zu prüfen ist, z. B. „Fehler 30172 (Schwimmerschalter hat ausgelöst) … Laut
Meldungsliste: Kondensatpumpe und den Kondensatablauf kontrollieren.“ Codes ab 50000 bedeuten, dass die
Wärmepumpe nach mehrfachem Auftreten verriegelt ist – dann nach dem Beheben zurücksetzen. Das Dashboard zeigt
dieselbe Erklärung im roten Streifen der Haustechnik.

## Wochenbericht (ab 0.50, ausführlich ab 0.51)

Jeden Sonntag um 18:12 schickt Core eine Zusammenfassung der letzten sieben Tage (Quelle „Wochenbericht“, Push an),
jeweils mit Vergleich zur Vorwoche:

- **Strom**: verbraucht und aus dem Netz mit Kosten, Solarertrag mit bestem Sonnentag, Einspeisung mit Vergütung,
  Autarkie. Zähler und Preise kommen aus dem Energie-Dashboard (fester Preis oder Preis-Entität).
- **Heizung**: Strom der Wärmepumpe – der Verbraucher im Energie-Dashboard mit „Wärmepumpe“ im Namen, sonst der
  Mittelwert ihres Leistungssensors aus der Haustechnik.
- **Wasser**: Wasserzähler des Energie-Dashboards.
- **Wetter**: mittlere, tiefste und höchste Außentemperatur (Sensor aus dem Katalog bzw. der Wetterstation).
- **Besondere Ereignisse**: Klingeln, Post, ausgelöster Alarm, erledigte Aufgaben, gemähte Flächen, gesaugte Räume.
- Was nächste Woche fällig wird.

Das Dashboard zeigt den Bericht bis Montagabend bei den Mitteilungen; antippen öffnet alle Zahlen. Websocket
`smarthome_ui/report/run` erstellt ihn sofort (`push` optional, `month: true` für die Monatsbilanz).

**Arbeitszahl der Wärmepumpe** (ab 0.52): Mit den Zählern für erzeugte Wärme und verbrauchten Strom (Heizen und
Warmwasser, wie die Haustechnik-Seite sie findet) steht im Bericht die Arbeitszahl der Woche mit Vorwoche. Core merkt
sich jeden Sonntag die Arbeitszahl mit der mittleren Außentemperatur (ein Jahr). Liegt sie deutlich (0,4) unter
Wochen mit ähnlichem Wetter (±2 °C), steht im Bericht „Achtung Wärmepumpe“ mit dem Hinweis, Heizkurve, Filter und
Abtauung zu prüfen.

**Heizkurven-Assistent** (ab 0.53): Alle 30 Minuten schaut Core, solange es draußen kühler als 15 °C ist, auf die
Thermostate im Heizbetrieb (Räume mit Soll ab 17 °C): wie weit jeder Raum vom Soll entfernt ist und ob das Thermostat
gerade heizt (zwei Wochen, nicht im Urlaubsmodus). Bleiben Räume zu kalt, obwohl die Thermostate heizen, ist die
Heizkurve zu tief; sind sie warm genug, obwohl die Thermostate kaum heizen (oder deutlich zu warm), ist die
Vorlauftemperatur höher als nötig – das kostet Arbeitszahl. Der Vergleich kalter (unter 3 °C) mit milden Tagen
(ab 8 °C) unterscheidet Steigung und Fußpunkt. Der Wochenbericht bekommt dann eine Zeile, z. B. „Heizkurve: Räume an
milden Tagen warm genug, Thermostate heizen nur 12 % der Zeit – Fußpunkt etwa 1 Grad tiefer“.

**Monatsbilanz** (ab 0.52): Am 1. jedes Monats um 9:12 kommt dieselbe Zusammenfassung für den Vormonat, verglichen
mit dem Monat davor und dem gleichen Monat im Vorjahr (Quelle „Wochenbericht“, Titel „Monatsbilanz November 2026“).
Das Dashboard zeigt sie drei Tage lang bei den Mitteilungen.

## Wächter (ab 0.54)

Core behält Home Assistant selbst im Blick (Quelle „Home Assistant“ `sys.health`, Push an):

- **Neustarts:** Jede Minute merkt sich Core, dass Home Assistant läuft. Startet es ohne vorher sauber
  heruntergefahren zu sein, hing es (der Watchdog des Supervisors hat neu gestartet) oder ist abgestürzt – dann
  kommt „Home Assistant ist neu gestartet, ohne vorher sauber herunterzufahren – etwa 5 Min. weg“.
- **Reaktionszeit:** Alle 10 Sekunden misst Core, wie spät ein Zeitgeber läuft. Ab 30 Sekunden zählt das als
  Hänger („Home Assistant hat 42 Sekunden lang nicht reagiert“, höchstens alle 6 Stunden).
- **Arbeitsspeicher und Last** jede Minute, je Stunde der schlechteste Wert (zwei Wochen); unter 5 % freiem
  Speicher eine Mitteilung.
- **Zigbee-Selbstheilung:** Ist Home Assistant oder der MQTT-Broker eine Weile weg, kann Zigbee2MQTT nicht
  melden, dass seine Geräte wieder da sind – sie bleiben „nicht verfügbar“, obwohl sie funktionieren. 5 Minuten
  nach einem Start (und 3 Minuten nachdem die Bridge wieder verbunden ist) zählt Core die nicht erreichbaren
  Zigbee-Geräte; sind es mehr als vorher, startet Core Zigbee2MQTT neu (`zigbee2mqtt/bridge/request/restart`,
  höchstens alle 6 Stunden) und meldet es. Abschaltbar im Dashboard.
- **Datenbank:** Einmal pro Woche nachts um 3:37 (und auf Knopfdruck) zählt Core – automatisch nur, wenn Home
  Assistant seit mindestens 2 Stunden läuft, gerade ruhig ist und genug Arbeitsspeicher frei hat (ab 0.55.1) –, wie viele Einträge jede Entität in den
  letzten 24 Stunden geschrieben hat, dazu die Größe der Datenbank (MariaDB/MySQL und SQLite). Im Dashboard lässt
  sich der Verlauf einer Entität auf 1 Tag kürzen (Core räumt ihn jede Nacht mit `recorder.purge_entities` auf,
  siehe „Verlauf je Gerät“) oder eine Diagnose-Entität abschalten.

Der Wochenbericht bekommt eine Zeile „System“, wenn etwas war (hing, reagierte nicht, Zigbee repariert).

Websocket: `smarthome_ui/guard/info`, `…/scan` (Datenbank jetzt zählen), `…/db_action` (`entity_id`,
`action`: `short` | `default` | `disable` | `enable`), `…/heal` (Zigbee2MQTT neu starten), `…/options`
(`heal`, `z2m_base`).

## System & Netzwerk

Gerät **„System“** mit `sensor.system_problems` (Anzahl, Attribut `problems`). Was überwacht wird, schickt das
Dashboard so, wie seine Seiten Netzwerk, Status und Haustechnik eingestellt sind. Core prüft jede Minute – auch ohne
offenes Dashboard:

- **Internet** (Quelle „Internet“): Verbindungs-Entität 2 Minuten nicht online.
- **Netzwerkgeräte** („Netzwerkgeräte“): Gateway, Switches und Accesspoints 5 Minuten nicht online („upgrading“,
  „provisioning“ und ähnliche Zustände zählen nicht). Mit **„UniFi-Geräte automatisch“** (Standard an) überwacht
  Core zusätzlich jedes Gerät der UniFi-Integration, auch wenn es auf der Netzwerk-Seite nicht eingetragen ist.
  **„Wieder online melden“**: „Gateway wieder da (12 min weg)“ bzw. „Internet wieder da“. **„Neustarts melden“**:
  Springt die Laufzeit eines Netzwerkgeräts zurück, kommt „Gateway neu gestartet“ – auch wenn es nur kurz weg war.
- **Dienste** („Dienste & Integrationen“): Integration eines Dienstes mit Fehler (Einrichtung fehlgeschlagen,
  Verbindung wird wiederholt, Migrationsfehler) oder Bridge offline (Zigbee2MQTT …); Statuszeilen der Seiten mit
  Fehlerzustand oder 10 Minuten nicht erreichbar.
- **Stromversorgung** („Stromversorgung (Raspberry Pi)“): Unterspannung (`rpi_power`).
- **Automationen** („Fehler in Automationen“): Der letzte Lauf einer Automation endete mit einem Fehler.


### Verlauf je Gerät (ab 0.48, länger ab 0.49)

Home Assistant hebt den Verlauf aller Entitäten gleich lange auf (`purge_keep_days`, Standard 10 Tage). Auf
der Dashboard-Seite *System* lässt sich ein **Standard für alle Geräte** und je Gerät eine eigene Dauer
wählen (1–365 Tage):

- **kürzer:** Core ruft jede Nacht um 04:17 (und kurz nach einer Änderung) `recorder.purge_entities` mit den
  Entitäten des Geräts auf,
- **länger:** Core hebt die Aufbewahrung des Recorders auf den längsten gewählten Wert an (zur Laufzeit; nach
  einem Neustart gilt kurz der konfigurierte Wert, Core hebt ihn gleich wieder an) und kürzt alle übrigen
  Entitäten nachts auf den Standard. Was schon gelöscht ist, kommt nicht zurück – längere Zeiten wirken ab jetzt.

Langzeitstatistiken (Stundenwerte, Energie-Dashboard) bleiben erhalten. Websocket: `smarthome_ui/history/set`
(`device_id`, `days`, `null` = Standard) und `smarthome_ui/history/default` (`days`, `null` = wie konfiguriert).

## Haushaltsgeräte

**„Haushaltsgerät hinzufügen“** (beliebig viele): Gerät eines Geschirrspülers, einer Waschmaschine, eines Trockners oder
Kühlschranks wählen (Home Connect, Miele, LG ThinQ, Electrolux …). Die Art erkennt Core am Namen, sie lässt sich aber
auch selbst wählen. Am Gerät findet Core Betriebszustand, Restzeit, Fortschritt, Programm, Startvorwahl, Tür und
Energiezähler – beim Kühlschrank Temperaturen (Kühl- und Gefrierteil), Türen und Alarm.

- **Läufe:** Start, Ende, Dauer, Programm und Verbrauch (kWh) des letzten Laufs; ab 5 Minuten zählt ein Lauf.
  Die übliche Dauer je Programm merkt sich Core – weicht ein Lauf stark ab, kommt eine Mitteilung.
- **Ausräumen** (Spüler, Waschmaschine, Trockner): Nach dem Lauf steht „Ausräumen“ an – mit Aufgabe
  „<Gerät> ausräumen“ in der Haushalts-Liste. Erledigt per Knopf „Ausgeräumt“, Abhaken (Liste oder Dashboard),
  Öffnen der Tür oder einem neuen Lauf.
- **Entitäten am Gerät:** `sensor.household_<gerät>_status` (Aus, Bereit, Wartet auf Start, Läuft, Fertig, Störung;
  Kühlschrank: OK, Tür offen, Zu warm, Alarm – Attribute Restzeit, Programm, Start, Temperaturen),
  `sensor.household_<gerät>_last_run`, `binary_sensor.household_<gerät>_to_unload`, `button.household_<gerät>_unloaded`.
- **Mitteilungen** (Quellen „Geschirrspüler“, „Waschmaschine & Trockner“, „Kühlschrank“, „Weitere Geräte“ mit Push an):
  fertig, Störung, Erinnerung ans Ausräumen (Standard nach 30 Minuten, 0 = nie), Sonnenstrom übrig, während das Gerät
  per Startvorwahl wartet (Sensor mit PV-Leistung oder Überschuss und Mindestwert, Standard 1500 W). Kühlschrank: Tür
  länger offen (Standard 3 Minuten), Kühlteil ab 8 °C bzw. Gefrierteil über −15 °C für 15 Minuten, Alarm,
  Wasserfilter (10 % oder weniger). Geschirrspüler: Spülmittel, Klarspüler oder Salz nachfüllen.
  Einstellbar unter *Smarthome UI Core → Haushaltsgerät → Zahnrad (ändern)*.

Das Dashboard nimmt Geschirrspüler und Kühlschrank dann aus Core; „Ausgeräumt“ gilt überall gleich.

## Wartungsteile

**„Wartungsteil hinzufügen“** (beliebig viele): Kohlefilter, Luftfilter, Dunstabzugsfilter, Rückspülfilter (am
Druckminderer des Hausanschlusses), Wasserfilter, Rauchmelder oder Sonstiges – mit Bereich und optional dem Gerät, zu
dem es gehört (z. B. der Lüfter im Bad). Ohne Gerät legt Core eines an („Kohlefilter Bad“) und ordnet es dem Bereich zu.

- **Zwei Intervalle** in Monaten, je 0 = aus, mit Vorschlag je Art: Reinigen bzw. Rückspülen bzw. Testen und Tauschen.

  | Art | Reinigen / Rückspülen / Testen | Tauschen |
  |---|---|---|
  | Kohlefilter | 3 | 12 |
  | Luftfilter | 1 | 6 |
  | Dunstabzugsfilter | 1 | – |
  | Rückspülfilter | 6 (rückspülen) | – |
  | Wasserfilter | – | 6 |
  | Rauchmelder | 12 (testen) | 120 |

- Tauschen zählt auch als gereinigt. Ohne bisheriges Datum zählt das Intervall ab dem Hinzufügen.
- **Entitäten am Gerät:** `sensor.household_<teil>_next_clean` / `_next_replace` (Datum; Attribute `last`, `days_left`,
  `due`) und Knöpfe `button.household_<teil>_cleaned` / `_replaced`.
- **Fällig:** „Kohlefilter Bad reinigen“ steht in der To-do-Liste „Haushalt“ und kommt zur Aufgaben-Uhrzeit als
  Mitteilung (Quelle „Wartung & Putzen“).
- **Umzug:** Wartungsaufgaben aus dem Dashboard, die nach einem solchen Teil klingen („Kohlefilter tauschen“,
  „Rauchmelder testen“ …), zieht Core einmalig um – je Art und Raum ein Wartungsteil, „reinigen“ und „tauschen“
  zusammengefasst, mit dem letzten Erledigt-Datum. Das Dashboard blendet die alten Aufgaben dann aus.

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

## Wasserverbrauch aus dem Enthärter

Ab 0.39: `sensor.technik_water_usage` (Gerät „Haustechnik“) zählt den Wasserverbrauch aus der **Restkapazität** des
Enthärters (Gerät mit Rolle „Enthärter“, z. B. SYR `getres`). Jede Abnahme um einen Liter ist Verbrauch, eine
Regeneration füllt die Restkapazität wieder auf und zählt nicht. Das ist genauer als den Durchfluss hochzurechnen:
Die SYR-Cloud liefert den Durchfluss nur etwa einmal pro Minute als Momentaufnahme, hochgerechnet zählt jeder Treffer
eine ganze Minute, und was zwischen zwei Abfragen fließt, fehlt. Der Sensor ist `total_increasing` in Litern und kann
im Energie-Dashboard als Wasserquelle eingetragen werden.

## Schalter-Tasten (Blueprint, ab 0.48)

`smarthome_ui/switch_buttons.yaml` belegt bis zu vier Tasten eines Schalters frei – je Taste getrennt für
**kurz drücken**, **lang drücken** und **gedrückt halten** (wiederholt, z. B. zum Dimmen). Gedacht für den
Hue Dimmer Switch (Hue-Integration: Ereignisse `short_release`, `long_press`, `repeat`), passt aber zu jedem
Schalter mit Ereignis-Entitäten; unter *Erweitert* lassen sich die Ereignisnamen anpassen. Schalter in
**Zigbee2MQTT** (dort gibt es standardmäßig keine Ereignis-Entitäten): unter *Zigbee2MQTT* den Namen des
Schalters eintragen – Core hört dann auf `zigbee2mqtt/<Name>/action` (`on_press_release` = kurz,
`*_hold_release` = lang, `*_hold` = gedrückt halten). Je Taste können
mehrere Entitäten gewählt werden (dieselbe Taste an mehreren Schaltern). Nur frische Ereignisse lösen aus,
ein Neustart von Home Assistant also nicht.

## Bewegungslicht (Vorschläge)

Ab 0.32: Core schlägt je Raum ein **Bewegungslicht** vor, wenn es dort Bewegungs- oder Präsenzmelder und Lichter gibt,
aber noch keine Automation, die diese Lichter über diese Melder schaltet. Das Dashboard zeigt die Vorschläge auf der
Seite Automationen und legt auf Knopfdruck eine **Home-Assistant-Automation** aus der Blaupause
`smarthome_ui/motion_light.yaml` an. Sie steht danach unter *Einstellungen → Automationen* und lässt sich dort ändern.

- Licht an bei Bewegung, aus nach einer einstellbaren Zeit ohne Bewegung (alle Melder des Raums ruhig). Ab 0.36 gibt es
  eine **Standard-Abklingzeit** am Core-Gerät „Licht“ (`number.lights_delay`, Standard 120 s). Sie gilt für jede
  Automation, deren eigene Zeit auf 0 steht. Eine eigene Zeit über 0 geht vor.
- Ab 0.37 lässt sich auch die **Helligkeit je Automation überschreiben** (`day_pct`, `night_pct` in %, 0 = zentral).
- Ab 0.40 geht statt eines Helligkeitssensors auch ein **Dunkel-Sensor** (`dark`, Ein/Aus, „an“ = dunkel, z. B. „Ist dunkel“ einer UniFi-Kamera).
- Ab 0.41 kann das Licht nach der Abklingzeit in den **Ausgangszustand** zurück (`restore`): Die Lichter kommen wieder so, wie sie vor der Bewegung waren, z. B. ein Grundlicht am Eingang wieder auf 10 % und die übrigen aus.
- Ab 0.38 **blendet das Licht sanft ein und aus**: zentral `number.lights_fade_on` / `number.lights_fade_off` (s, Standard 1 s, 0 = hart schalten), je Automation `fade_on` / `fade_off` (-1 = zentral).
- Optional nur, wenn ein Helligkeitssensor unter einem Lux-Wert liegt (oder das Licht schon an ist). Ab 0.35 wartet
  die Automation dann bis zu 30 s auf einen neuen Messwert: Direkt nach dem Ausschalten meldet der Sensor oft noch die
  Helligkeit mit Licht, sonst bliebe das Licht bei schneller Rückkehr aus.
- **Helligkeit zentral** am Core-Gerät „Licht“: `number.lights_brightness_day` und `number.lights_brightness_night` (%),
  die Nacht von `time.lights_night_start` bis `time.lights_night_end` (Standard 22:00–06:30). Eine Änderung gilt sofort
  für alle Räume.
- Als „schon abgedeckt“ zählt jede Automation, die einen Melder des Raums (Entität oder Gerät, z. B. Geräte-Auslöser)
  und ein Licht des Raums verwendet – als Entität, Gerät, über den Bereich, eine Lichtgruppe oder eine Szene mit
  diesem Licht. Auch selbst gebaute zählen. Ab 0.33 erkennt Core das über die Bezüge, die Home Assistant für jede
  Automation kennt, nicht nur über den Text.
- Einen Vorschlag kannst du **ausblenden** (`lights/ignore`), z. B. wenn ein Raum bewusst ohne Bewegungslicht bleibt.
- **Label und Kategorie** (ab 0.33): Automationen aus den Blaupausen von Core bekommen das Label **Smart-Core** und eine
  Kategorie nach Anwendung („Licht“ für das Bewegungslicht, „Saugen“ für „Saugen, wenn alle weg sind“) – nach dem Start
  und nach jedem Neuladen der Automationen. Eine selbst gewählte Kategorie bleibt.

## Saugen, wenn alle weg sind

Core bringt die Aktion **`smarthome_ui.vacuum_rooms`** mit: saugt die gewählten Bereiche jetzt – nur Räume auf der Etage
des Saugers, auf Wunsch nur saugen (Wischen aus) und ohne Bereiche, die in den letzten *N* Stunden gesaugt wurden. Läuft
der Sauger gerade oder hat er einen Fehler, passiert nichts. Antwort: `{"started": [...]}`.

Dazu legt Core eine **Vorlage (Blueprint)** an: *Einstellungen → Automationen & Szenen → Blueprints →
„Bereiche saugen, wenn alle weg sind“*. Eingaben: Saugroboter, Bereiche (z. B. Eingangsbereich), Wartezeit nach dem
Gehen (Standard 5 min), Auslassen, wenn kürzlich gesaugt (4 h), Zeitfenster (7–21 Uhr). Ausgelöst wird, wenn
`zone.home` auf 0 fällt – also wenn der Letzte das Haus verlässt. Im Dashboard lässt sich die Automation in der
Saugroboter-Kachel anlegen, ein- und ausschalten.

## Heizung: Thermostat-Displays und PV-Überschuss

Drei weitere **Vorlagen (Blueprints)** unter *Einstellungen → Automationen & Szenen → Blueprints*:

- **Thermostat-Display nach Uhrzeit** – dimmt die Displays der Raumthermostate (Meross LAN: Helligkeit „Bedienung“
  und „Standby“) nachts und macht sie morgens wieder hell. Eingaben: Thermostate (Geräte), Nacht ab / Morgens ab,
  Helligkeit tagsüber und nachts (Schritte von 12,5 %; Standby 0 = aus).
- **Thermostat-Display nach Anwesenheit** – hell, sobald jemand da ist (Präsenz-/Bewegungsmelder an, Person oder
  Gerät zuhause, Schalter an – eins reicht), dunkel, wenn eine Weile niemand mehr da ist (Standard 10 min).
  Optional eine Nachtruhe, in der es immer dunkel bleibt (beide Zeiten gleich = keine).
- **Estrich mit PV-Überschuss laden** – für Fußbodenheizung mit Wärmepumpe: speist die Anlage eine Weile mehr als
  eingestellt ein (Netzleistung unter z. B. −1500 W für 10 min, Batterie optional mindestens 80 %, Startfenster
  10–14:30 Uhr), hebt sie die Sollwerte der Raumthermostate **und** der Wärmepumpe (z. B. „Tagbetrieb Temperatursoll“
  – eine Wärmepumpe mit Heizkurve heizt sonst nicht mehr) um 1 K an. Zur Endzeit (16:30) oder bei Netzbezug über
  800 W für 15 min stellt sie die vorherigen Werte wieder her (Momentaufnahme; nach einem Neustart um den gleichen
  Betrag zurück). Ein Merker-Schalter (`input_boolean`) hält fest, dass gerade geladen wird – einmal am Tag.

- **Warmwasser mit Sonnenstrom** (ab 0.53) – speist die Anlage eine Weile mehr als eingestellt ein (z. B. −1000 W für
  10 min, Batterie optional mindestens 90 %, Startfenster 10–15 Uhr), hebt sie den Warmwasser-Sollwert der
  Wärmepumpe (`number`, z. B. „Tagbetrieb Warmwasser Temperatursoll“) auf das Ziel (55 °C). Zur Endzeit (18 Uhr) oder
  bei Netzbezug über 800 W für 15 min kommt der vorherige Wert zurück (Momentaufnahme; nach einem Neustart der
  eingestellte Normalwert, z. B. 50 °C). Ein Merker-Schalter (`input_boolean`) hält fest, dass gerade geladen wird.

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
| `smarthome_ui/get` | Daten (`done`, `done_log`, `household`, `mow_log`), Mäh- und Saugroboter, Haushaltsgeräte, Putz-Intervalle, Status |
| `smarthome_ui/subscribe` | Ereignis bei jeder Datenänderung |
| `smarthome_ui/done` | Abhaken / wieder öffnen / Rückgängig (`changes`, `log`, `unlog`) |
| `smarthome_ui/data/set` | `household` bzw. `mow_log` ersetzen |
| `smarthome_ui/tasks/sync` | Aufgaben mit Fälligkeit |
| `smarthome_ui/notify/config` | Regeln, Empfänger, Quellen, Ruhezeiten, Uhrzeit für Aufgaben |
| `smarthome_ui/notify/report` | aktuelle Meldungen eines offenen Dashboards |
| `smarthome_ui/notify/log` | Mitteilungen der letzten 30 Tage (`items`: `id`, `t`, `title`, `message`, `src`, `url`, `urgent`, `status`, `actions`, `answer`) |
| `smarthome_ui/newdev/info` | neue Geräte mit Vorschlägen (`suggest_area`, `suggest_name`, `diagnostics`, `device_class`, `sim`) |
| `smarthome_ui/newdev/apply` | Vorschläge übernehmen (`device_id`, `area_id`, `name`, `enable`, `device_class`, `sim`; nur Admins) |
| `smarthome_ui/newdev/dismiss` | neues Gerät so lassen (`device_id`) |
| `smarthome_ui/windows/info` | Fenster (Rollladen, Kontakt, Himmelsrichtung, Putzstatus, Sonne, Hitzeschutz) und Einstellungen |
| `smarthome_ui/windows/set` | Fenster anlegen (ohne `window_id`) oder ändern (`name`, `area_id`, `cover`, `contact`, `lock`, `door`, `orientation`, `auto`) |
| `smarthome_ui/windows/remove` | Fenster entfernen (`window_id`) |
| `smarthome_ui/windows/clean` | Fenster geputzt (`window_ids`) |
| `smarthome_ui/windows/settings` | Automatiken (`morning`, `morning_weekend`, `morning_on`, `evening_on`, `evening_offset`, `heat_on`, `heat_temp`, `heat_position`, `lockout`, `winter`, `outdoor`, `clean_days`, ab 0.56 `trigger` time/dark, `dark_sensor`, `dark_delay` Minuten, `stagger` Sekunden) |
| `smarthome_ui/notify/log_action` | Aktion einer Mitteilung nachträglich auslösen (`log_id`, `action`) – wie am Handy getippt |
| `smarthome_ui/mower/settings` | Mähplan eines Mähers (`plan` je Monat, `interval`, `freq` je Fläche) |
| `smarthome_ui/alarm/settings` | Alarm-Logik übernehmen (`panel`, `rules`, `actions`, `guests`, `nightwalk`, einmalig `guests_on`, `nightwalk_until`) |
| `smarthome_ui/security/settings` | Fenster & Türen (`openings`) und Nacht (`night`: `start`, `end`, Sensorlisten je Art) |
| `smarthome_ui/mailbox/settings` | Briefkasten-Sensoren (`flap`, `door`, `enabled`) |
| `smarthome_ui/presence_sim/settings` | Anwesenheitssimulation (`enabled`, `lights`, `covers`, `start`, `from`, `until`, `min`, `max`, `parallel`, `when`, `covers_open`) |
| `smarthome_ui/grocery/settings` | Einkauf: Sensor „letzter Monat“ (`entity`), einmalig bisherige Monate (`months`) |
| `smarthome_ui/technik/settings` | Haustechnik-Sensoren und Schwellen (`heatpump_error`, `inverter_error`, `inverter_error_ok`, `reach`, `pressure`, `pressure_min`, `pressure_critical`, `pressure_drop`, `salt`, `salt_max`, `syr`, `flow`, `valve`, `water_minutes`, `compressor`, `cycles_per_hour`, `power`) |
| `smarthome_ui/system/settings` | System & Netzwerk (`internet`, `devices`, `status`, `services`, `automations`) |
| `smarthome_ui/catalog/link` | Gerät einer Rolle zuordnen (`device_id`, `role`, optional `name`) |
| `smarthome_ui/catalog/suggest` | Einrichtung: passende Geräte je Rolle, bestes zuerst (`roles`: `device_id`, `name`, `manufacturer`, `model`, `entities`, `score`, `current`) |
| `smarthome_ui/devices/suggest` | Einrichtung: Mähroboter, Saugroboter, Haushaltsgeräte und Klingeln in Home Assistant (`added` = schon hinzugefügt) |
| `smarthome_ui/devices/add` | mehrere davon auf einmal hinzufügen (`items`: `type`, `device_id`, optional `name`, `kind`, `camera`, `lock`) |
| `smarthome_ui/catalog/unlink` | Gerät einer Rolle wieder entfernen (`role`) |
| `smarthome_ui/onboarding/done` | Einrichtungsassistent abgeschlossen (`done`, Standard `true`; `false` = noch einmal anbieten) |
| `smarthome_ui/lists/set` | eine Liste einer Seite setzen (`page`, `path`, `items` – `null` = automatisch) |
| `smarthome_ui/lists/value` | eine einzelne Entität setzen (`path`, `value` – `null` = automatisch) |
| `smarthome_ui/lights/suggest` | Räume mit Bewegungsmeldern und Lichtern, die abdeckende Automation (`automation`, `ours` = aus der Blaupause) und die zentralen Einstellungen |
| `smarthome_ui/lights/ignore` | Vorschlag eines Raums ausblenden oder wieder zeigen (`area_id`, `ignore`, Standard `true`) |
| `smarthome_ui/lights/settings` | Tag-/Nachthelligkeit (`day`, `night` in %), Nachtzeiten (`night_start`, `night_end`, „HH:MM“) und Standard-Abklingzeit (`delay` in s) |
| `smarthome_ui/ui/check` | neue Dashboard-Version suchen (Antwort mit `notes` seit der installierten) |
| `smarthome_ui/ui/install` | neueste Dashboard-Version installieren (nur Admins) |
| `smarthome_ui/ui/dashboard` | YAML-Datei eines Dashboards (`url_path`); mit `update: true` holt Core `dashboards/smarthome.yaml` neu aus GitHub (nur Admins) |
| `smarthome_ui/ui/notes` | Zustand und Neuerungen seit der installierten Version |
| `smarthome_ui/plants/settings` | Düngen nach Kalender je Pflanze (`entity`, `feed`: `every` Wochen, `from`/`to` Monat) |
| `smarthome_ui/cleaning/settings` | eigene Putz-Intervalle je Raum (`intervals`) |
| `smarthome_ui/display/connect` | offenes Dashboard als Bildschirm anmelden; Befehle (`set`, `reload`, `reconnect`) kommen als Ereignisse |
| `smarthome_ui/display/update` | Zustand eines Bildschirms (Seite, Theme, Jahreszeit, Töne, Leistung, Ruhemodus, Benutzer) |

## Entwicklung

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```
