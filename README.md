# Maus-Klicker

Maus- und Tastatur-Abläufe aufnehmen, bearbeiten und abspielen, um ein anderes
Programm automatisch zu steuern (Windows).

Ein Schritt im Ablauf ist einer von drei Typen:

| Typ | Was passiert |
|---|---|
| **Klick** | Maus fährt zu einer Position und klickt (links/rechts/Mitte, 1–3 Klicks) |
| **Text** | Ein Text wird Zeichen für Zeichen getippt |
| **Taste** | Eine Taste oder Tastenkombination wird gedrückt, z. B. `Enter`, `Strg+S`, `Alt+F4` |

## Installation

Benötigt Python 3.8+ (mit tkinter, ist bei der normalen Python-Installation dabei).

```
pip install -r requirements.txt
python clicker.py                 # verwendet sequence.json neben dem Skript
python clicker.py meine_aufgabe.json    # oder eine andere Ablauf-Datei
```

### Als .exe bauen

```
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name Maus-Klicker --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 clicker.py
```

Die fertige `dist\Maus-Klicker.exe` braucht kein installiertes Python. Sie
speichert ihre `sequence.json` im selben Ordner wie die exe.

## Programmfenster verfolgen

Klicke auf **🎯 Programm wählen** und dann auf das Programm, das gesteuert
werden soll. Ab dann werden Klicks, die du in diesem Programm aufnimmst (auch in
seinen Dialogen), **relativ zur oberen linken Ecke des Fensterinhalts**
gespeichert statt als absolute Bildschirmposition. Beim Abspielen wird das
Fenster wieder gesucht und seine aktuelle Position dazugerechnet. Das
funktioniert also auch, wenn das Fenster verschoben wurde oder auf einem
anderen Bildschirm liegt (auch eine andere Anzeigeskalierung wird ausgeglichen).

- Neben dem Programm siehst du, ob das Fenster gefunden wurde, wo sein Anker
  liegt und welche Skalierung der Bildschirm hat.
- Das Fenster wird über den Prozessnamen gefunden (z. B. `notepad.exe`),
  bevorzugt mit gleicher Fensterklasse und gleichem Titel. Es funktioniert
  also auch nach einem Neustart des Programms.
- Die Spalte **Koordinaten** zeigt pro Klick-Schritt `Fenster` oder
  `Bildschirm`. Doppelklick darauf (oder **Fenster / Bildschirm**) rechnet die
  markierten Schritte um.
- Wählst du ein Programm für einen Ablauf, der schon Schritte mit
  Bildschirmposition hat, wird angeboten, sie umzurechnen.
- **Beim Abspielen in den Vordergrund** holt das Programmfenster vor dem
  Abspielen nach vorne.
- Der Anker ist die obere linke Ecke: Verschieben geht immer, *Größe ändern*
  nur, wenn die angeklickten Elemente dabei an ihrer Stelle bleiben.

## Tastatur

- **Aufnehmen:** Mit *Tastatur mitaufnehmen* (Standard: an) zeichnet
  **●● Mehrere aufnehmen** auch die Tastatur auf. Getippte Zeichen werden zu
  einem Text-Schritt zusammengefasst (Rücktaste korrigiert ihn),
  Sondertasten und Kombinationen mit Strg/Alt/Win werden zu Tasten-Schritten.
  Eingaben im Klicker-Fenster selbst sowie `Esc`, `F8` und `F9` werden nicht
  aufgenommen.
- **Von Hand hinzufügen:** **+ Text...** oder **+ Taste...**. Tasten werden
  mit `+` verbunden. Deutsche und englische Namen gehen beide: `Strg`/`Ctrl`,
  `Umschalt`/`Shift`, `Alt`, `Win`, `Enter`, `Tab`, `Esc`, `Entf`, `Rücktaste`,
  `Leertaste`, `Pos1`, `Ende`, `Bild↑`/`page_up`, Pfeiltasten (`↑` oder `up`),
  `F1`–`F24`, Buchstaben und Ziffern. Für die Plus-Taste `Plus` schreiben.
- **Abspielen:** Tastatureingaben gehen an das aktive Fenster. Ist ein
  Programm gewählt, wird vor jedem Tastatur-Schritt geprüft, dass es im
  Vordergrund ist (sonst wird es nach vorne geholt). Ohne Programm wird
  zumindest sichergestellt, dass nie in den Klicker selbst getippt wird.
  *Tippverzögerung* ist die Pause zwischen zwei getippten Zeichen.

## Sicherheit: Abbruch bei Eingabe

Mit **Bei Eingabe abbrechen** (unter *Abspielen*, Standard: an) stoppt das
Abspielen sofort, sobald du selbst die Maus bewegst, klickst, das Mausrad
drehst oder eine Taste drückst. Danach erscheint eine Meldung, was erkannt
wurde (z. B. „Abgebrochen: Benutzereingabe erkannt - Mausbewegung bei
(600, 500)“). Die Meldung steht auch in der Statuszeile und, wenn das Skript
in einer Konsole läuft, dort.

- Die Überwachung beginnt erst **nach der Startverzögerung**. Während des
  Countdowns kannst du also noch zum Programm wechseln.
- Die eigenen Mausbewegungen, Klicks und Tastendrücke des Abspielens lösen
  keinen Abbruch aus.
- **Esc** stoppt wie bisher ohne Meldung.

## Wartezeiten und Dauer

- **Standard:** Jeder Schritt wartet vorher die **Standard-Wartezeit**
  (unten bei *Abspielen*).
- **Eigene Wartezeiten der Schritte verwenden** (Standard: aus): Dann wartet
  jeder Schritt seine eigene Wartezeit aus der Spalte *Eigene Wartezeit*, z. B.
  die bei der Aufnahme gemessene Pause. Solange die Option aus ist, stehen die
  eigenen Werte in Klammern, z. B. `(1.25)` – sie bleiben gespeichert, werden
  aber nicht benutzt.
- **Wartezeit = Standard** setzt die eigene Wartezeit der markierten Schritte
  auf die Standard-Wartezeit (ist nichts markiert, nach Rückfrage für alle).
- Neben der Schrittzahl steht die **erwartete Dauer** eines Durchlaufs (aktive
  Schritte: Wartezeiten + Mausbewegung + Tippen, ohne Startverzögerung), bei
  mehreren Wiederholungen auch die Gesamtdauer.

## Bedienung

| Aktion | So geht's |
|---|---|
| Zu steuerndes Programm wählen | **🎯 Programm wählen**, dann in dessen Fenster klicken |
| Einen Klick aufnehmen | **● Klick aufnehmen**, dann im Programm klicken |
| Mehrere Klicks + Tastatur aufnehmen | **●● Mehrere aufnehmen**, im Programm klicken und tippen, dann **Esc** oder **■ Aufnahme stoppen** |
| Position ohne Klick hinzufügen | Maus aufs Ziel und **F8** drücken |
| Text / Taste hinzufügen | **+ Text...** / **+ Taste...** |
| Wert bearbeiten | Doppelklick auf die Zelle (Bezeichnung, X, Y, Aktion, Eigene Wartezeit) |
| Umbenennen | Zeile markieren und **F2** drücken |
| Kompletter Editor (Typ ändern, „Mausposition in 3 s übernehmen" ...) | Doppelklick auf `#` oder `Typ`, **Enter** oder **Bearbeiten...** |
| Schritt an-/ausschalten | Doppelklick auf die Zelle `An` oder **Leertaste** |
| Reihenfolge ändern | Zeilen ziehen, **Strg+↑ / Strg+↓** oder die Pfeil-Buttons |
| Duplizieren / Löschen | **Strg+D** / **Entf** (Mehrfachauswahl mit Strg/Umschalt) |
| Abspielen | **▶ Abspielen** oder **F9** (funktioniert aus jedem Fenster) |
| Stoppen | **Esc** (funktioniert aus jedem Fenster) |

Hinweise:
- Neue Schritte werden nach der markierten Zeile eingefügt (sonst am Ende).
- Klicks auf das Klicker-Fenster selbst werden bei der Aufnahme ignoriert, und
  beim Abspielen wird nie auf den Klicker selbst geklickt.
- Mit *Zeit zwischen Schritten aufnehmen* wird die echte Pause zwischen deinen
  Eingaben als eigene Wartezeit des Schritts gespeichert (wird nur benutzt, wenn
  *Eigene Wartezeiten der Schritte verwenden* an ist).
- Zwei schnelle Klicks an derselben Stelle werden als ein Doppelklick gespeichert.
- *Wiederholen* `0` wiederholt endlos, bis du Esc drückst.
- Läuft das gesteuerte Programm als Administrator, muss auch `clicker.py` als
  Administrator gestartet werden, sonst blockiert Windows Klicks und Tastatur.

## Dateiformat

Alles wird nach jeder Änderung automatisch gespeichert. Pro Schritt stehen nur
die Felder in der Datei, die für seinen Typ wichtig sind:

```json
{
  "version": 3,
  "settings": {
    "repeat": 1,
    "start_delay": 3.0,
    "move_duration": 0.2,
    "default_delay": 0.5,
    "use_custom_delays": false,
    "type_delay": 0.03,
    "record_timing": true,
    "record_keyboard": true,
    "minimize_on_play": true,
    "abort_on_input": true,
    "bring_to_front": true
  },
  "program": {
    "process": "notepad.exe",
    "title": "Unbenannt - Editor",
    "class": "Notepad"
  },
  "events": [
    {
      "label": "Ins Textfeld klicken",
      "type": "click",
      "x": 412,
      "y": 230,
      "relative": true,
      "button": "left",
      "clicks": 1,
      "delay": 0.5,
      "enabled": true
    },
    {
      "label": "Namen eingeben",
      "type": "text",
      "text": "Max Mustermann",
      "delay": 0.5,
      "enabled": true
    },
    {
      "label": "Speichern",
      "type": "key",
      "keys": "ctrl+s",
      "delay": 0.5,
      "enabled": true
    }
  ]
}
```

- `delay` ist die eigene Wartezeit in Sekunden *vor* dem Schritt. Sie gilt nur
  bei `"use_custom_delays": true`, sonst wartet jeder Schritt `default_delay`.
- `button` ist `left`, `right` oder `middle`.
- `"relative": false`: `x`/`y` sind absolute Bildschirmpixel.
- `"relative": true`: `x`/`y` sind der Abstand zur Ecke des Fensterinhalts, in
  Pixeln bei 100 % Skalierung (bei 150 % sind 100 also 150 echte Pixel).
- `keys` verwendet die englischen Tastennamen von pynput (`ctrl`, `shift`,
  `alt`, `cmd` = Win, `enter`, `page_up`, `f5`, ...), verbunden mit `+`.
- `"program": null` bedeutet, dass kein Programm gewählt ist. Ältere Dateien
  (Version 1 und 2) lassen sich weiterhin öffnen; Schritte ohne `type` sind Klicks.

## Lizenz

[MIT](LICENSE)
