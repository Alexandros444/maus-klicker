# Maus-Klicker

Mausklick-Abläufe aufnehmen, bearbeiten und abspielen, um ein anderes Programm
automatisch zu steuern (Windows).

## Installation

Benötigt Python 3.8+ (mit tkinter, ist bei der normalen Python-Installation dabei).

```
pip install -r requirements.txt
python clicker.py                 # verwendet sequence.json neben dem Skript
python clicker.py meine_aufgabe.json    # oder eine andere Ablauf-Datei
```

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
- Die Spalte **Koordinaten** zeigt pro Schritt `Fenster` oder `Bildschirm`.
  Doppelklick darauf (oder **Fenster / Bildschirm**) rechnet die markierten
  Schritte um.
- Wählst du ein Programm für einen Ablauf, der schon Schritte mit
  Bildschirmposition hat, wird angeboten, sie umzurechnen.
- **Beim Abspielen in den Vordergrund** holt das Programmfenster vor dem
  Abspielen nach vorne.
- Der Anker ist die obere linke Ecke: Verschieben geht immer, *Größe ändern*
  nur, wenn die angeklickten Elemente dabei an ihrer Stelle bleiben.

## Bedienung

| Aktion | So geht's |
|---|---|
| Zu steuerndes Programm wählen | **🎯 Programm wählen**, dann in dessen Fenster klicken |
| Einen Klick aufnehmen | **● Klick aufnehmen**, dann im Programm klicken |
| Mehrere Klicks aufnehmen | **●● Mehrere aufnehmen**, durch das Programm klicken, dann **Esc** oder **■ Aufnahme stoppen** |
| Position ohne Klick hinzufügen | Maus aufs Ziel und **F8** drücken |
| Wert bearbeiten | Doppelklick auf die Zelle (Bezeichnung, X, Y, Taste, Klicks, Warten) |
| Umbenennen | Zeile markieren und **F2** drücken |
| Kompletter Editor (inkl. „Mausposition in 3 s übernehmen") | Doppelklick auf die `#`-Zelle, **Enter** oder **Bearbeiten...** |
| Schritt an-/ausschalten | Doppelklick auf die Zelle `An` oder **Leertaste** |
| Reihenfolge ändern | Zeilen ziehen, **Strg+↑ / Strg+↓** oder die Pfeil-Buttons |
| Duplizieren / Löschen | **Strg+D** / **Entf** (Mehrfachauswahl mit Strg/Umschalt) |
| Abspielen | **▶ Abspielen** oder **F9** (funktioniert aus jedem Fenster) |
| Stoppen | **Esc** (funktioniert aus jedem Fenster) |

Hinweise:
- Neue Schritte werden nach der markierten Zeile eingefügt (sonst am Ende).
- Klicks auf das Klicker-Fenster selbst werden bei der Aufnahme ignoriert, und
  beim Abspielen wird nie auf den Klicker selbst geklickt.
- Mit *Zeit zwischen Klicks aufnehmen* wird die echte Pause zwischen deinen
  Klicks als Wartezeit des Schritts gespeichert.
- Zwei schnelle Klicks an derselben Stelle werden als ein Doppelklick gespeichert.
- *Wiederholen* `0` wiederholt endlos, bis du Esc drückst.
- Läuft das gesteuerte Programm als Administrator, muss auch `clicker.py` als
  Administrator gestartet werden, sonst blockiert Windows die Klicks.

## Dateiformat

Alles wird nach jeder Änderung automatisch gespeichert:

```json
{
  "version": 2,
  "settings": {
    "repeat": 1,
    "start_delay": 3.0,
    "move_duration": 0.2,
    "default_delay": 0.5,
    "record_timing": true,
    "minimize_on_play": true,
    "bring_to_front": true
  },
  "program": {
    "process": "notepad.exe",
    "title": "Unbenannt - Editor",
    "class": "Notepad"
  },
  "events": [
    {
      "label": "Menü öffnen",
      "x": 412,
      "y": 230,
      "relative": true,
      "button": "left",
      "clicks": 1,
      "delay": 0.5,
      "enabled": true
    }
  ]
}
```

- `delay` ist die Wartezeit in Sekunden, *bevor* die Maus zu diesem Schritt fährt.
- `button` ist `left`, `right` oder `middle` (in der Oberfläche: links, rechts, Mitte).
- `"relative": false`: `x`/`y` sind absolute Bildschirmpixel.
- `"relative": true`: `x`/`y` sind der Abstand zur Ecke des Fensterinhalts, in
  Pixeln bei 100 % Skalierung (bei 150 % sind 100 also 150 echte Pixel).
- `"program": null` bedeutet, dass kein Programm gewählt ist. Ältere Dateien
  ohne diese Felder lassen sich weiterhin öffnen; ihre Schritte sind
  Bildschirmpositionen.
