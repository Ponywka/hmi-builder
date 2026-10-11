"""Loads a portable project directory (see docs/PORTABLE_FORMAT.md) for the emulator."""

from __future__ import annotations

import json
import os
import re

from .lang import HmiError

_PLACEHOLDER = re.compile(r"\$\{(page|picture|font|obj|pagename|objname|animation):([^}]+)\}")


class ProjectError(Exception):
    pass


class ObjDef:
    """One component (or the page object, id 0) as written in the project."""

    def __init__(self, page, key, type_, attrs, events, ident):
        self.page = page
        self.key = key
        self.type = type_
        self.attrs = attrs
        self.events = events
        self.id = ident
        self.name = str(attrs.get("objname", key))


class PageDef:
    def __init__(self, index, key, name, root, objects):
        self.index = index
        self.key = key
        self.name = name
        self.root = root            # ObjDef, id 0, type "page"
        self.objects = objects      # [ObjDef], ids 1..n
        self.by_name = {o.name: o for o in objects}
        self.all = [root] + objects


class Project:
    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        path = os.path.join(self.dir, "project.json")
        try:
            with open(path, encoding="utf-8") as f:
                self.config = json.load(f)
        except (OSError, ValueError) as exc:
            raise ProjectError("cannot read %s: %s" % (path, exc))
        self.picture_keys = [p["key"] for p in self.config.get("pictures", [])]
        self.font_keys = [p["key"] for p in self.config.get("fonts", [])]
        self.animation_keys = [p["key"] for p in self.config.get("animations", [])]
        self.page_keys = [p["key"] for p in self.config.get("pages", [])]
        self.pictures = {p["key"]: p.get("source", {}).get("png") for p in self.config.get("pictures", [])}
        self.fonts = {p["key"]: p.get("source", {}).get("zi") for p in self.config.get("fonts", [])}
        self.animations = {a["key"]: [f.get("png") for f in a.get("frames", [])] for a in self.config.get("animations", [])}
        self.animation_ms = {a["key"]: [int(f.get("ms", 50)) for f in a.get("frames", [])] for a in self.config.get("animations", [])}
        raws = [self._read_page(i, entry) for i, entry in enumerate(self.config.get("pages", []))]
        self._objid = {}
        for raw in raws:
            self._objid[raw["key"]] = {(o.get("key") or o.get("attributes", {}).get("objname")): n + 1
                                       for n, o in enumerate(raw["objects"])}
        self._objname = {}
        for raw in raws:
            self._objname[raw["key"]] = {(o.get("key") or o.get("attributes", {}).get("objname")):
                                         str(o.get("attributes", {}).get("objname", o.get("key")))
                                         for o in raw["objects"]}
        self.pages = [self._build_page(i, raw) for i, raw in enumerate(raws)]
        self.page_by_name = {p.name: p for p in self.pages}
        self.page_by_key = {p.key: p for p in self.pages}
        self.program_lines = self._program()

    # -- reading ---------------------------------------------------------------------------------------------------

    def _read_page(self, index, entry):
        content = entry.get("content", {})
        mode = content.get("mode")
        if mode == "json":
            with open(os.path.join(self.dir, content["path"]), encoding="utf-8") as f:
                data = json.load(f)
        elif mode == "inline":
            data = content
        else:
            raise ProjectError("page %r is stored as %r, which the emulator cannot read" % (entry.get("key"), mode))
        data = dict(data)
        data["key"] = entry["key"]
        data.setdefault("name", entry["key"])
        data.setdefault("objects", [])
        data.setdefault("root", {"attributes": {}, "events": {}})
        return data

    def _program(self):
        name = self.config.get("program")
        if not name:
            return []
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return []
        return [self._code_line(line, None) for line in text.split("\n")]

    # -- references ------------------------------------------------------------------------------------------------

    def _ref_value(self, value):
        if isinstance(value, dict) and "$ref" in value:
            kind, _, key = value["$ref"].partition(":")
            table = {"picture": self.picture_keys, "font": self.font_keys, "page": self.page_keys,
                     "animation": self.animation_keys}.get(kind)
            if table is None or key not in table:
                raise ProjectError("unknown reference %s" % value["$ref"])
            return table.index(key)
        if isinstance(value, dict) and "hex" in value:
            return value["hex"]
        return value

    def _code_line(self, line, page_key):
        if not isinstance(line, str):
            line = line.get("text", "")

        def sub(m):
            kind, arg = m.group(1), m.group(2)
            try:
                if kind == "page":
                    return str(self.page_keys.index(arg))
                if kind == "picture":
                    return str(self.picture_keys.index(arg))
                if kind == "font":
                    return str(self.font_keys.index(arg))
                if kind == "animation":
                    return str(self.animation_keys.index(arg))
                if kind == "pagename":
                    return self.config["pages"][self.page_keys.index(arg)].get("name") or arg
                pk, _, ok = arg.partition("/")
                if kind == "obj":
                    return "0" if ok == "(page)" else str(self._objid[pk][ok])
                if kind == "objname":
                    return self._objname[pk][ok]
            except (ValueError, KeyError):
                raise ProjectError("unknown reference %s" % m.group(0))
            return m.group(0)

        return _PLACEHOLDER.sub(sub, line)

    def _build_page(self, index, raw):
        key = raw["key"]

        def make(o, ident, type_):
            attrs = {k: self._ref_value(v) for k, v in o.get("attributes", {}).items()}
            events = {e: [self._code_line(ln, key) for ln in lines] for e, lines in o.get("events", {}).items()}
            return attrs, events

        attrs, events = make(raw["root"], 0, "page")
        attrs.setdefault("objname", raw["name"])
        attrs.setdefault("x", 0)
        attrs.setdefault("y", 0)
        root = ObjDef(key, "(page)", "page", attrs, events, 0)
        objects = []
        for n, o in enumerate(raw["objects"], 1):
            attrs, events = make(o, n, o.get("type"))
            okey = o.get("key") or attrs.get("objname") or "obj%d" % n
            attrs.setdefault("objname", okey)
            objects.append(ObjDef(key, okey, o.get("type", "button"), attrs, events, n))
        return PageDef(index, key, raw["name"], root, objects)

    # -- resources -------------------------------------------------------------------------------------------------

    def path(self, rel):
        return os.path.join(self.dir, rel)
