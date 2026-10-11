# HMI emulator

`tools/hmi_emulator.py` runs a portable project directory (see `PORTABLE_FORMAT.md`) as if it were the display: it
executes `Program.s` and the event code of the pages, answers the host's instructions, draws the screen (with the
project's own pictures and `.zi` fonts) and lets a browser act as the touch panel. The UART is exposed as a pseudo
terminal, a TCP port or a unix socket, so the program that normally talks to the display can talk to the emulator
instead. Edit the project, save, and the emulator reloads: no `.HMI`, `.tft` or flashing in between.

It needs Python 3 and Pillow (`pip install Pillow`). Nothing from the vendor's editor is used.

## Quick start

    python3 tools/hmi_emulator.py path/to/project-dir --pty --watch

prints the pseudo terminal (`UART: pty /dev/pts/N`) and the address of the web UI (`http://127.0.0.1:8765/`). Open the
page: the screen is shown at the top left (click = touch, drag moves sliders), the UART traffic is logged on the right
(orange = host -> display, green = display -> host), and there are boxes to send an instruction as the host
(`t0.txt="hello"`) or raw bytes to the host (`65 03 01 ff ff ff`), a page selector, a power-cycle button and the
current variables.

| option | meaning |
|---|---|
| `--pty [LINK]` | open a pty; `LINK` is an optional symlink to it, e.g. `--pty /dev/ttyS1` (needs permission to create it) |
| `--tcp [HOST:]PORT` / `--unix PATH` | serve the UART on a socket; `socat pty,raw,echo=0,link=/tmp/ttyS1 tcp:127.0.0.1:PORT` makes a pty from it |
| `--http [HOST:]PORT` / `--http off` | web UI, default `127.0.0.1:8765` |
| `--watch` | reload when a `.json`, `.s`, `.png` or `.zi` file of the project changes; stays on the same page and keeps the variables; a broken edit is reported in the log and the old project keeps running |
| `--state-dir DIR` | keep the EEPROM (`wepo` / `repo`) between runs; without it the EEPROM is empty (reads as -1) at every start |
| `--bkcmd N` | initial return-code mode (default 2: only errors are answered, like `bkcmd=2`) |
| `--no-boot-frame` | do not send `00 00 00 ff ff ff` and `88 ff ff ff` at power-up |

## Connecting the program under test

The program must open the emulator's pty (or socket) in place of the real serial port. The simplest way is the
symlink the program already uses: `--pty /dev/ttyS1` (run as a user that may write to `/dev`, or
`sudo ln -s /dev/pts/N /dev/ttyS1` after starting). Baud rate settings are accepted and ignored. Example for the xindi
port in this setup: run xindi (in the docker test image or on the printer, not on the development host) with the
emulator's pty or a `socat` link in place of `/dev/ttyS1`, and edit `display_firmware` with `--watch` running.

The emulator speaks the protocol of the display: instructions end with `ff ff ff`; replies are `01` (ok, bkcmd 1/3),
`1a` invalid variable, `03` invalid page, `1b` invalid operation, `00` invalid instruction (bkcmd 2/3), `70`/`71` for
`get`, `66` for `sendme`; `twfile` (file transfer) is stored in memory and shown by `external_picture` components;
`whmi-wri` (firmware download) is acknowledged and discarded (the project is what runs).

## What is emulated

* **Language** (`lang.py`): `if / else if / else`, `for`, `while`, `+ - * / %`, `&& ||`, comparisons, `+= -= *= /= %=`,
  `++ --`, text concatenation, `text -= N` (drop the last N characters), `p[id].b[id].attr` references, `page.obj.attr`,
  globals (`int` in `Program.s`), system variables (`dp dim dims bkcmd sleep thsp thup delay loadpageid loadcmpid ...`),
  `type` and `id` of a component. Commands: `page vis tsw click prints printh print get sendme wepo repo covx cov btlen
  strlen substr spstr rest delay ref cls pic picq xstr fill line draw cir cirs`, `obj.write()` / `obj.close()`.
* **Events**: `codesload`, `codesloadend`, `codesunload`, `codesdown`, `codesup`, `codesslide`, `codestimer` (timers of
  the current page; minimum period 50 ms), `touch_capture` (fires on every touch). A touch goes to the top-most visible,
  touch-enabled component that has event code (or is a slider); otherwise to the page object. A page change inside an
  event ends that event.
* **Page state**: components of a page start from their stored values every time the page is loaded; components with
  `vscope=1` keep their values.
* **Drawing**: page background (solid / picture), button, text, number (crop image, solid colour, image, border),
  crop picture, progress bar, slider, animation (frames from the project), external picture, text with the project's
  fonts (alignment, line breaks, word wrap, spacing), `dim` and `sleep`.

## Differences from the real display (read before trusting a result)

* It is an interpretation of the documented behaviour, not the firmware: details such as drawing order of overlapping
  alpha pictures, text clipping and rounding may differ. Verify a final change on the display.
* No timing: the emulator is not slowed down to the baud rate and instructions run instantly. `delay` really sleeps.
* Not drawn: `col_pic` (thumbnail data is kept but shown as a grey box), gauges, waveforms, video/audio, QR codes.
* Not implemented: RTC, GPIO, serial pass-through, `play`/`stop` (accepted, no effect), the `sendkey`/send-component-id
  tick boxes (the project uses explicit `prints`), touch calibration, the text EEPROM instructions beyond storing the given value (`rept`/`wept`).
* Unknown instructions are answered with `00` and logged; unknown names with `1a`.

## Tests

`python -m unittest discover -s tools/tests` includes `test_hmi_emu.py`, which builds a small synthetic project and
checks the language, touch, timers, pages, rendering, the pty and TCP links and reloading.
