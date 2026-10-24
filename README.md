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
| `smarthome_ui/get` | Daten (`done`, `done_log`, `household`, `mow_log`), Mäh- und Saugroboter, Haushaltsgeräte, Putz-Intervalle, Status |
| `smarthome_ui/subscribe` | Ereignis bei jeder Datenänderung |
| `smarthome_ui/done` | Abhaken / wieder öffnen / Rückgängig (`changes`, `log`, `unlog`) |
| `smarthome_ui/data/set` | `household` bzw. `mow_log` ersetzen |
| `smarthome_ui/tasks/sync` | Aufgaben mit Fälligkeit |
| `smarthome_ui/notify/config` | Regeln, Empfänger, Quellen, Ruhezeiten, Uhrzeit für Aufgaben |
| `smarthome_ui/notify/report` | aktuelle Meldungen eines offenen Dashboards |
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
| `smarthome_ui/ui/check` | neue Dashboard-Version suchen (Antwort mit `notes` seit der installierten) |
| `smarthome_ui/ui/install` | neueste Dashboard-Version installieren (nur Admins) |
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
