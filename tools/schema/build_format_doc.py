#!/usr/bin/env python3
"""Writes PORTABLE_FORMAT.md: the description of the portable project format for people and LLMs.

python3 schema/build_format_doc.py [portable-project-dir] > PORTABLE_FORMAT.md

The narrative is fixed; the appendices (component reference, page table, fonts, animations, startup script) are generated
from hmi_schema.json and, when a directory is given, from that project.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hmi_parse as H  # noqa: E402

NARRATIVE = r'''# Portable project format (USART HMI / TJC display projects)

This describes the source-only project directory written by `python3 hmi_project.py unpack --portable PROJECT.HMI DIR`
and built by `python3 hmi_project.py pack DIR OUT.HMI --config project.json`. The format is for the USART HMI editor
(Nextion-compatible, Chinese TJC/"USART HMI" clone) that targets TJC4827X243_011-class touch displays. The directory is
the source of truth: everything needed to rebuild the project file is in it, nothing is compiled.

Who reads this: a person or an LLM that has the project directory and must change the **host-side software** (the
"backend" that talks to the display over UART) so that it matches the screens, or change the screens themselves.
Section 7 explains the display <-> host protocol as it appears in the project; section 9 lists the safe editing rules.

## 1. Directory layout

```
DIR/
  project.json        order of pages / pictures / fonts / animations, per-type attribute layouts, project header
  Program.s           startup script (runs once at power-on: global variables, then `page 0`)
  pages/<key>.json    one file per page: the page object and all its components with attributes and event code
  pictures/<key>.png  one PNG per picture (compiled `.i` / `.is` files are generated at build time)
  fonts/<key>.zi      compiled fonts (binary; there is no TTF source for the existing fonts)
  animations/<key>/NNN.png   animation frames (compiled `.gmov` / `.gmovs` are generated at build time)
```
All paths are relative to `DIR`. Keys are the file stems (letters, digits, `_`, `.`, `-`).

## 2. IDs: the position in a list is the ID (important for the host protocol)

Nothing in the files stores a numeric ID of a page, picture, font, animation or component. The runtime IDs are:

| Thing | ID is | Range |
|---|---|---|
| page | index in `project.json` -> `pages` (0-based) | 0..254 (255 = "none" in swipe attributes) |
| picture | index in `pictures` | 0..65534 (65535 = "none") |
| font | index in `fonts` | 0..255 |
| animation | index in `animations` | 0..65534 (65535 = "none") |
| component | 0 for the page object, then 1, 2, ... = position in the page's `objects` list | 0..254 |

So **reordering, inserting or deleting an entry changes the IDs of everything after it**. The UART frames sent by the
display (section 7) contain the runtime page ID (`dp`) and sometimes component IDs, so the host software that interprets
them depends on this order. A reference is therefore always written symbolically (section 5) and resolved at build time.

## 3. `project.json`

```json
{
  "format": "usart-hmi-project-v2",
  "portable": true,
  "project":  {"header": "<96 bytes hex>", "index_order": ["i", "zi", "gmov", "pa"]},
  "layouts":  {"98": [["type", 1], ["id", 1], ["objname", "t"], ...], "...": []},
  "pages":      [{"key": "main", "content": {"mode": "json", "path": "pages/main.json"}}],
  "pictures":   [{"key": "pic_10", "source": {"png": "pictures/pic_10.png"}}],
  "fonts":      [{"key": "font_0", "source": {"zi": "fonts/font_0.zi"}}],
  "animations": [{"key": "anim_0", "fps": 10, "frames": [{"png": "animations/anim_0/000.png", "ms": 500}]}],
  "program": "Program.s"
}
```
* `pages`, `pictures`, `fonts`, `animations`: the complete ordered lists; the index is the runtime ID. A page entry may
  also be inline: `{"key": ..., "content": {"mode": "inline", "name": ..., "root": {...}, "objects": [...]}}`.
* A font may instead be generated from a TTF/OTF: `"source": {"ttf": "fonts/x.ttf", "height": 24, "chars": "ABC...", "layout": "bmp", "bpp": 3}`
  (needs Pillow and fontTools). An animation without a usable source is `{"key", "gmov": path, "gmovs": path}` (binary, copied).
* `project.header`: the 96-byte project header (device model, orientation, passwords...). Do not edit.
* `layouts`: how each component type is stored in the binary file: ordered `[attribute, width]`, width = 1/2/4 bytes
  (integer), `"t"` (text), `"h"` (hex bytes). **Do not edit.** A component with a different layout carries its own `layout`.
  Components created from scratch for the types page, button, text, picture, timer, variable use built-in defaults instead.
* `project.index_order`: the order of resource kinds inside the project index. Do not edit.

## 4. Page file `pages/<key>.json`

```json
{
  "name": "main",
  "header": {"lock": "0000000000552100", "writer": "01410500000000000000000000000000"},
  "root": {"attributes": {"x": 0, "y": 0, "w": 272, "h": 480, "up": 255, "down": 255, "left": 255, "right": 255,
                            "sta": 2, "bco": 65535, "pic": {"$ref": "picture:pic_10"}},
           "events": {"codesload": [], "codesloadend": [], "codesdown": [], "codesup": [], "codesunload": []}},
  "objects": [
    {"key": "b0", "type": "button",
     "attributes": {"objname": "b0", "x": 8, "y": 303, "w": 166, "h": 60, "txt": "OK", "font": {"$ref": "font:font_0"}, "...": 0},
     "events": {"codesdown": [], "codesup": ["prints 0x65,1", "prints dp,1", "prints 3,1", "prints 0xff,1", "prints 0xff,1", "prints 0xff,1"]}}
  ]
}
```
* `name`: the page name (<= 16 bytes). It is also the page object's `objname`. Code can use it: `page main`, `main.n0.val`.
* `root`: the page object itself (type 121, id 0, managed). Its attributes are the page background (`sta`: 0 none /
  1 solid colour `bco` / 2 picture `pic`) and the four swipe targets `up/down/left/right` (a page reference, 255 = none).
  Its events are `codesload` (before the page is drawn), `codesloadend` (after), `codesdown`, `codesup` (touch on the
  background), `codesunload` (when leaving).
* `objects`: components in z-/id order (position = component ID, starting at 1). Each has a unique `key` (any stable
  name; it is only used for references), a `type` (name below or number), `attributes` and `events`.
  `objname` is the component's name used in code (`b0`, `t0`...) and must be unique within the page.
* Attribute values: integers (width from `layouts`), strings (UTF-8), `{"hex": "..."}` for raw bytes, `{"$ref": ...}` for
  references. `type` and `id` are managed and not written. `endx`/`endy` are omitted when they equal `x+w-1` / `y+h-1`
  (they are derived). Colours are RGB565 integers (e.g. 65535 white, 0 black, 63488 red, 2016 green, 31 blue).
  Coordinates are in the display's own orientation (see the project's width and height), origin top-left.
* `events`: event name -> list of code lines (the instruction language of section 6). Event names are `codesdown` (touch
  press), `codesup` (touch release), plus type-specific ones (`codestimer` for timers, `codesplayend` for animations,
  `codesslide` for sliders). Blocks that exist for a component must stay (empty lists are fine).
* Optional per-object fields kept only when they differ from the default: `layout`, `extra` (table word) and `tail`
  (hex trailer). Leave them alone.

Component types the builder knows (name = value of `type`): `button`, `text`, `number`, `variable`,
`timer`, `crop_picture`, `animation`, `touch_capture`, `touch_hotspot`, `sliding_text`, `slider`, `progress_bar`,
`col_pic`, `external_picture`; new components can be created from scratch for `button`, `text`, `picture`, `timer`,
`variable` (give `objname`, `x`, `y`, `w`, `h` and whatever else differs from the defaults).
Appendix A lists the attributes of every type with widths, defaults and meaning.

## 5. References (how IDs are written)

Attributes (any attribute that holds a picture / font / page / animation ID):

| Written as | Meaning |
|---|---|
| `{"$ref": "picture:KEY"}` | picture ID of `pictures[KEY]` (attributes `pic`, `picc`, `pic1`, `pic2`, `picc1`, `picc2`, `bpic`, `ppic`) |
| `{"$ref": "font:KEY"}` | font ID (attribute `font`) |
| `{"$ref": "page:KEY"}` | page ID (swipe attributes `up`/`down`/`left`/`right` of the page object) |
| `{"$ref": "animation:KEY"}` | animation ID (attribute `vid` of an `animation` component) |
| `65535` (pictures, animations), `255` (pages) | "none" sentinel, written as a plain number |

Plain numbers in these attributes that fall inside the valid ID range are rejected by the build.

Event code (`Program.s` and every line of `events`): a placeholder `${...}` is replaced by the final number at build time:

| Placeholder | Becomes |
|---|---|
| `${page:KEY}` | page ID, e.g. `page ${page:main}` |
| `${picture:KEY}` / `${font:KEY}` | picture / font ID, e.g. `b0.pic=${picture:pic_10}`, `t0.font=${font:font_1}` |
| `${obj:PAGEKEY/OBJKEY}` | component ID on that page (`OBJKEY` = the object's `key`; `(page)` = the page object, 0), e.g. `vis ${obj:main/b0},1` |
| `${pagename:KEY}` / `${objname:PAGEKEY/OBJKEY}` | the page name / component name |

Names written literally (`page main`, `b0.txt="x"`, `main.n0.val`) are not IDs and stay as they are. Numbers written
literally in recognised ID positions (`page 3`, `b[2].pic=5`, `pic 0,0,7`, `vis 2,1`, `tsw 2,0`, `.pic==5`) are rejected,
because they would silently break when the order changes; use placeholders. Computed IDs (`p[loadpageid.val]`) cannot be
checked: the page ID must come from the host or from `dp` (the current page) at run time.

## 6. Event code language (instruction set of the display)

Lines are executed by the display firmware, one instruction per line, `//` starts a comment. It is the Nextion
instruction set with TJC extensions. Commonly used:

* assignment: `t0.txt="text"`, `n0.val=5`, `b0.pic=3`, `h0.val=h0.val+1`; arithmetic `+ - * / %`, comparison `== != < >`,
  `&& ||`, text concatenation with `+`; `covx`/`cov` convert number<->text; `substr`, `strlen` for text.
* control flow: `if(cond){ ... }else if(cond){ ... }else{ ... }`, `for(i.val=0;i.val<n;i.val++){ ... }`, `while`.
* page change: `page NAME` or `page ID` (also `page dp` = reload the current page, `page loadpageid.val`).
* component control: `vis OBJ,0|1` (hide/show), `tsw OBJ,0|1` (touch enable), `click OBJ,0|1` (fire its down/up event),
  `ref OBJ` (redraw), `ref_stop`, `ref_star`, `delay=N` (ms), `play`, `setlayer`, `cls COLOR`, `xstr`, `pic X,Y,ID`, `picq X,Y,W,H,ID`,
  `xpic ...`, `fill`, `draw`, `line`, `cirs`...
* storage: `repo VAR,ADDR` / `wepo VAR,ADDR` read/write the display's EEPROM (e.g. persistent settings), `rest`.
* UART output: `prints EXPR,LEN` sends `LEN` bytes of the value (LEN 0 = a text), `printh 91` sends hex bytes, `print` sends
  text, `get OBJ.val` / `sendme` answer a read. Every frame ends with three `0xff` bytes.
* UART input: the host can send any of these instructions (terminated by `0xff 0xff 0xff`) to the display, e.g.
  `t0.txt="22.5"`, `page 3`, `n0.val=40`, `ref b0`, `vis t3,1`; and it can set globals such as `lang.val=2`.
* system variables: `dp` current page ID, `sleep`, `dim`, `dims` (backlight), `bauds` (baud rate), `thsp`/`thup` (auto sleep),
  `sys0..sys2` (scratch globals).
* Variables: globals are declared in `Program.s` (`int sys0=0,lang=0,...`, 4-byte signed only) or are components of type
  `variable` with `vscope=1` (global). A component with `vscope=0` is only visible from its own page; from another page
  address it as `pagename.objname.val`.

Text with non-ASCII characters is UTF-8 in the project and is sent/stored as UTF-8 (the project header encoding is utf-8).

## 7. Display -> host protocol

The display talks to the host only through `prints` / `printh` lines in event code (plus the replies to `get`/`sendme`).
A common convention (verify it against the project you are given) is that a button's `codesup` sends a frame of the form

```
prints 0x65,1      -> 0x65            frame marker (same value as the Nextion "touch event" return code)
prints dp,1        -> <page ID>       dp = the runtime ID of the current page (index in project.json pages!)
prints N,1         -> <action code>   N is chosen by the HMI author per button (NOT the component ID)
prints 0xff,1 x3   -> 0xff 0xff 0xff  terminator
```
i.e. the host receives `65 <page> <action> ff ff ff`. The action code identifies "what was pressed" in the author's own
numbering (small integers chosen per button). Some frames carry more bytes: a number sent as 2 bytes (`prints obj.val,2`,
`prints page.obj.val,2`, or a constant such as `prints 80,2`) or a text (`prints obj.txt,0`). Frames may also send values of components of *other* pages, and a page may send a single byte
(`printh 91`) from its `codesload`.

To list all frames of a project quickly:

```bash
python3 hmi_project.py pack DIR /tmp/x.HMI --config project.json
python3 hmi_parse.py /tmp/x.HMI --project -o /tmp/project.json   # per object: "tx" (frames made only of prints), "navigation"
grep -h -E 'prints|printh' DIR/pages/*.json                       # or search the source directly
```
When the backend changes, derive the table "(page ID, action code) -> meaning" from (a) the position of the page in
`project.json` -> `pages` (that is `dp`), and (b) the `prints N,1` constant in the button that sends it. Do **not** use
a component's position/ID as the action code: they are unrelated numbers.

Host -> display: the backend sends instructions such as `page N`, `x.txt="..."`, `x.val=N`, `ref x`, `vis x,0`; each is
terminated by `0xff 0xff 0xff`. The display answers with the standard Nextion return codes (`0x01` success when `bkcmd` is
on, `0x1a` invalid variable, `0x70 <text> ff ff ff` string reply to `get`, `0x71 <4 bytes LE> ff ff ff` number reply). Names used by the
backend (`t0`, `n0`, `page.n2`...) are the `objname` values and page names of the project, so renaming a
component or a page breaks the backend that addresses it.

## 8. Building and checking

```bash
python3 hmi_project.py pack DIR OUT.HMI --config project.json     # builds OUT.HMI (about 20 s; never overwrites)
python3 hmi_parse.py OUT.HMI --check                              # structural check: prints OK
python3 hmi_parse.py OUT.HMI --images PNGDIR                      # (optional) pictures back out of a built project
```
The build reports precise errors: unknown references (`Unknown reference ${page:x}`), plain numbers where a reference is
required, duplicate names/keys, invalid attributes. Pictures need Pillow; no other tool is needed. The result is a
`.HMI` project file; use the editor's "File -> Output production file" to produce the `.tft` firmware for the screen.

## 9. Rules for editing a project directory

1. Never write numeric IDs of pages/pictures/fonts/animations/components in `$ref` positions or code; use keys/placeholders.
2. Keep every `key` unique and stable; rename a key only together with every reference to it.
3. To **add** a page: add `{"key": "x", "content": {"mode": "inline", "name": "x", "objects": [...]}}` (or a new
   `pages/x.json` + `"mode": "json"`) at the position where it should appear in `pages`. Position = page ID, so inserting
   in the middle shifts the IDs of later pages (and therefore the `dp` value in their UART frames). Appending keeps all IDs.
4. To **add a component**: append to the page's `objects` (keeps the IDs of the others) with at least `key`, `type`,
   `objname`, `x`, `y`, `w`, `h`; events as lists of code lines. `objname` must be unique on the page.
5. To **delete** something, remove its list entry; every reference to it must be removed or changed first (the build lists
   the leftovers as unknown references).
6. Do not touch `layouts`, `project.header`, `header`, `extra`, `tail`, `root` attribute `type`/`id`.
7. Text attributes: `txt_maxl` is the capacity in bytes; `txt` must not be longer.
8. If a UART frame is added or changed, update the host side in the same change (action code table in section 7).
9. Code lines must be valid for the display's instruction set (section 6); the build does not check the syntax of
   the code, only the references inside it.

## 10. Limits

At most 255 pages and 254 components per page; at most 65,535 pictures; 256 fonts; 15,000 files in the container.
Pictures are RGB565 with 7-bit alpha, all frames of an animation have the same size. An animation entry may set `"opaque": true` (compiled without alpha streams; then every pixel must be opaque). Compiled fonts and `.tft` are not
produced from text; there is no UART/hardware test in this tooling.
'''

TABLE_HEADER = '| attribute | bytes | default | description |\n|---|---|---|---|\n'


def default_text(attr):
    raw = bytes(attr['default'])
    if attr['name'] in ('txt', 'objname', 'path'):
        return '"%s"' % raw.decode('utf8', 'replace')
    if len(raw) in (1, 2, 4):
        signed = attr['name'] in H.SIGNED16 and len(raw) == 2 or attr['name'] in H.SIGNED32 and len(raw) == 4
        return str(int.from_bytes(raw, 'little', signed=signed))
    return raw.hex() or '-'


def component_reference(types):
    out = ['## Appendix A. Component reference (from the editor\'s own tables, series X2)\n',
           'Every visual component starts with this common head (in this order): `objname`, `vscope` (0 page-private, 1 global), '
           '`drag`, `sendkey`, `aph` (opacity 0-127), `movex`, `movey`, `x`, `y`, `w`, `h`, `endx`, `endy` (derived), `effect`, `first`, '
           '`time`, `lockobj`, `groupid0`, `groupid1`. Timers and variables have only `objname`, `vscope`, `lockobj`, `groupid0`, `groupid1`. '
           'The tables list the type-specific attributes that follow.\n']
    for code in types:
        sch = H.SCHEMA.get(str(code))
        if not sch:
            continue
        ev = ', '.join('`codes%s`' % e['label'] for e in sch['events']) or 'none'
        out.append('### %s (type %d, prefix `%s`)\nEvents: %s\n' % (sch['name_en'], code, sch['default_prefix'], ev))
        if sch.get('attributes'):
            out.append(TABLE_HEADER)
            for a in sch['attributes']:
                desc = re.sub(r'\s+', ' ', a['description_en']).replace('|', '/')
                if not a['editor_visible']:
                    desc += ' (hidden in the editor, still stored)'
                width = len(a['default']) if a['name'] not in ('txt', 'path') else 'text'
                out.append('| `%s` | %s | %s | %s |\n' % (a['name'], width, default_text(a), desc))
        out.append('\n')
    return ''.join(out)


def project_appendix(directory):
    d = Path(directory)
    cfg = json.loads((d / 'project.json').read_text(encoding='utf8'))
    out = ['## Appendix B. This project\n']
    out.append('### Pages (ID = list position)\n\n| ID | key | name | components |\n|---|---|---|---|\n')
    for i, p in enumerate(cfg['pages']):
        doc = json.loads((d / p['content']['path']).read_text(encoding='utf8'))
        out.append('| %d | `%s` | %s | %d |\n' % (i, p['key'], doc['name'], len(doc['objects'])))
    out.append('\n### Fonts\n\n| ID | key |\n|---|---|\n')
    for i, f in enumerate(cfg['fonts']):
        out.append('| %d | `%s` |\n' % (i, f['key']))
    out.append('\n### Animations\n\n| ID | key | frames | ms per frame |\n|---|---|---|---|\n')
    for i, a in enumerate(cfg['animations']):
        frames = a.get('frames', [])
        out.append('| %d | `%s` | %s | %s |\n' % (i, a['key'], len(frames) or '(binary)', ', '.join(sorted({str(f['ms']) for f in frames})) or '-'))
    out.append('\n%d pictures: `pictures/*.png`, ID = position in `project.json` -> `pictures`.\n' % len(cfg['pictures']))
    program = (d / cfg['program']).read_text(encoding='utf8', errors='replace') if cfg.get('program') else ''
    out.append('\n### Startup script `Program.s` (global variables and first page)\n\n```\n%s\n```\n' % program.strip())
    return ''.join(out)


def main():
    directory = sys.argv[1] if len(sys.argv) > 1 else None
    present = [121, 98, 116, 54, 51, 52, 113, 2, 5, 62, 70, 60, 106, 1, 109, 112]
    sys.stdout.write(NARRATIVE + '\n' + component_reference(present))
    if directory:
        sys.stdout.write('\n' + project_appendix(directory))


if __name__ == '__main__':
    main()
