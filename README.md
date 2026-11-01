# bk2318-printer

Driver and GUI for the **Brüel & Kjær 2318** thermal printer with the **ZI 0054** serial module. One Python file: graphical interface, command line and library. Covers every command in the 1988 instruction manual.

## Install

**Windows, no Python:** download `bk2318-printer.exe` from [Releases](https://github.com/stoneset/bk2318-printer/releases) and run it.

**From source:**

```
python -m pip install -r requirements.txt
python b2318.py
```

Use `python -m pip`, not `pip`: otherwise packages may go to another Python ("No module named 'serial'"). The package is `pyserial`, not `serial`.

## Wiring (cable n°AO 0404 from B&K)

| LEMO (ZI 0054) | PC DB9 | Signal |
|---|---|---|
| 3 | 2 RxD | printer -> PC (XON/XOFF) |
| 5 | 3 TxD | PC -> printer |
| 2 | 8 CTS | HS, ready/busy |
| 4 | 5 | ground |

- The USB adapter must output real RS-232 levels, not TTL.
- Factory settings: 1200 baud, XON/XOFF, 7-bit data. In 7-bit mode XON/XOFF arrive as 0x91/0x93; the program handles it. Check the cable with `python b2318.py diag`.
- Power: 4 NiMH AA cells. **Never** connect the charger without batteries: above 6 V the print head burns out.
- 4800 baud is about 4× faster for images (DIP switches N1-1 to N1-4, manual 4.1.5).

## Interface

<img width="862" height="672" alt="python_psqC7AdCf6" src="https://github.com/user-attachments/assets/fc4d1b1a-27de-4bb2-8ab3-32d24fc40c5e" />

Tabs: **Text**, **Templates**, **Charts** (bar chart, lines, calibration), **Image**, **Custom characters** (7×7 glyph editor), **Tools** (character sets, demo page, reset, raw bytes).

Footer:

- **Status:** "Transmission complete." means all data has been sent. The printer then still prints its 7 KB buffer; the remaining time covers that too.
- **Remaining time:** measured transmission rate + printing at 0.8 lines/s. Adjust the speed in Charts > Printer calibration if needed.
- **LEDs:** left = port open; right = flashes on each packet sent or received.
- New prints are refused until the current one is finished. **Stop** cancels the sending, or unlocks the interface once everything has been sent.

## Command line

```
python b2318.py -p COM4 text "Hello"
python b2318.py -p COM4 text --title TITLE --wide --inverse --align center "..."
python b2318.py -p COM4 template measurement_report --set operator=VD
python b2318.py -p COM4 image photo.png
python b2318.py -p COM4 bars "63 Hz=72" "125 Hz=68" --unit dB
python b2318.py -p COM4 sample --all
python b2318.py --dry-run demo          # show the bytes, send nothing
python b2318.py -h                      # every command and option
```

## Text templates

UTF-8 text files in `templates/`, editable in any text editor. Import from the Templates tab or `template --import file.txt`.

| Syntax | Effect |
|---|---|
| plain line | printed as is |
| `# comment` | ignored (the first one is the description) |
| `{name}` | field asked when printing; `{date}` `{time}` `{datetime}` are automatic |
| `@field name "Label" "default"` | label and default value of a field |
| `@title TEXT` | banner |
| `@kv Label \| Value \| .` | label left, value right, optional fill |
| `@rule full\|dotted\|dashed\|dotdash` | line across the paper |
| `@feed N` | empty lines |
| `@wide`, `@inverse` `on\|off`, `@align left\|center\|right` | styles |
| `@table` … `@end` | table, cells separated by `\|` |
| `@bars unit=dB min=0 max=100` … `@end` | bar chart, `label = value` lines |
| `@image file.png [width=120] [threshold=128]` | image |
| `@charset runes`, `@raw ESC 28 4D`, `@eot`, `@reset` | low level |

Example: `templates/measurement_report.txt`.

<img height="400" alt="IMG_1166" src="https://github.com/user-attachments/assets/8aaf6e78-1186-4c41-be76-5a9eb7802bdd" />

## Credits and related work

- **FrodeM** scanned the manual and found the hidden rune set.
- **saschapo** found the 7-bit XON/XOFF issue, measured the line pitch and read the PDI command table. His repository [saschapo/bk2318-esp32](https://github.com/saschapo/bk2318-esp32) has an **ESP32-C6 wireless adapter**, **Code 128 barcodes** drawn with PDI dot columns, **bold text** by overprinting with a 1-dot shift, a lowercase Cyrillic DRC set and his bench notes.


## Sources

- [Instruction manual 2318 + ZI 0054, archive.org](https://archive.org/details/b-k-2318-zi-0054-instruction-manual)
- [EEVblog thread: runes in the B&K 2318](https://eevblog.com/forum/testgear/why-is-there-runes-in-my-bruel-kjaer-2318-serial-printer)
