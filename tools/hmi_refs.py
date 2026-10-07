#!/usr/bin/env python3
"""Typed reference handling for structural HMI edits (standard library only).

Numbers that appear in a project mean different things: picture/font/page/component ids that move when entities are
inserted, deleted or reordered, but also UART payload bytes, colours, geometry and arithmetic. Nothing is rewritten by
guessing: only attribute fields and code forms with a known meaning are remapped, once, from the *baseline* numbering
(the ids as unpacked) to the *final* numbering. Everything else that could depend on a changed id is reported as a
finding; the compiler turns findings into errors unless the config acknowledges them explicitly.

Code forms understood (comments and string literals are masked, spans are rewritten in place so formatting survives):
  page <n|name>                       p[n].b[m]   b[m]
  <x>.pic|pic1|pic2|picc|picc1|picc2|bpic|ppic <op> <n>     <x>.font <op> <n>      (equality is remapped, others flagged)
  pic x,y,n   picq x,y,w,h,n   xpic x,y,w,h,x0,y0,n   xstr x,y,w,h,font,...
  vis|tsw|click|ref|setlayer <n>,...  (component id; 255 = all stays)
`${page:key}` `${picture:key}` `${font:key}` `${obj:page/obj}` `${pagename:key}` `${objname:page/obj}` placeholders are
expanded to final values after remapping and are never remapped themselves.
"""
import re

PIC_ATTRS = ('pic', 'pic1', 'pic2', 'picc', 'picc1', 'picc2', 'bpic', 'ppic')
PAGE_SWIPE = ('up', 'down', 'left', 'right')
PIC_NONE, PAGE_NONE = 65535, 255
# Component types whose picture/page references have ordered, packed or mode-dependent semantics that are not modelled.
UNMODELLED_TYPES = {65: 'file browser (icon text)', 125: 'VP keyboard', 126: 'VP keyboard', 127: 'artistic text VP',
                    128: 'text VP', 129: 'data VP', 130: 'progress VP', 131: 'picture VP', 135: 'popup menu VP',
                    136: 'icon carousel VP'}
ID_COMMANDS = ('vis', 'tsw', 'click', 'ref', 'setlayer')
PLACEHOLDER = re.compile(r'\$\{(page|picture|font|obj|pagename|objname):([^}]*)\}')


class Finding:
    __slots__ = ('kind', 'domain', 'text', 'message')

    def __init__(self, kind, domain, text, message):
        self.kind, self.domain, self.text, self.message = kind, domain, text, message

    def __repr__(self):
        return 'Finding(%s/%s: %s)' % (self.kind, self.domain, self.message)


class Maps:
    """Frozen baseline -> final id maps (None = deleted) and the name tables used to detect dangling names."""

    def __init__(self, page, pic, font, comp, page_names_base, page_names_final, comp_names_base, comp_names_final,
                 global_names):
        self.page, self.pic, self.font, self.comp = page, pic, font, comp
        self.page_names_base, self.page_names_final = page_names_base, set(page_names_final)
        self.comp_names_base, self.comp_names_final = comp_names_base, comp_names_final
        self.global_names = set(global_names)

    @staticmethod
    def _identity(m):
        # Deleted entries (None) only matter for static references (reported as dangling); computed references can
        # only be invalidated for survivors whose id moves.
        return all(v is None or k == v for k, v in m.items())

    def changed(self, domain, page=None):
        if domain == 'comp':
            if page is not None:
                return not self._identity(self.comp.get(page, {}))
            return any(not self._identity(m) for m in self.comp.values())
        return not self._identity({'page': self.page, 'pic': self.pic, 'font': self.font}[domain])


def mask(text):
    """Same-length copy with comments/strings blanked and `${...}` placeholders replaced by \\x02."""
    out = list(text)
    for m in PLACEHOLDER.finditer(text):
        for i in range(m.start(), m.end()):
            out[i] = '\x02'
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if out[i] == '\x02':
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] not in '"\n':
                j += 2 if text[j] == '\\' and j + 1 < n and text[j + 1] != '\n' else 1
            end = j + 1 if j < n and text[j] == '"' else j
            for k in range(i, end):
                if out[k] != '\x02':
                    out[k] = ' '
            i = end
        elif c == '/' and text[i:i + 2] == '//':
            j = text.find('\n', i)
            j = n if j < 0 else j
            for k in range(i, j):
                if out[k] != '\x02':
                    out[k] = ' '
            i = j
        else:
            i += 1
    return ''.join(out)


def _split_args(code, start, end):
    """Top-level comma split of code[start:end]; returns [(a, b)] spans."""
    spans, depth, a = [], 0, start
    for i in range(start, end):
        c = code[i]
        if c in '([':
            depth += 1
        elif c in ')]':
            depth -= 1
        elif c == ',' and depth == 0:
            spans.append((a, i))
            a = i + 1
    spans.append((a, end))
    return spans


def _strip(code, a, b):
    while a < b and code[a].isspace():
        a += 1
    while b > a and code[b - 1].isspace():
        b -= 1
    return a, b


class Rewriter:
    """Rewrites one code text (an event line or Program.s). `page` is the owning baseline page id or None."""

    def __init__(self, maps, page=None, own_comp=None, strict=False, own_names=None, symbolize=None, forbid=False):
        """symbolize(domain, value, comp_page) -> '${...}' or None turns known ids into placeholders (export);
        forbid=True reports every known id written as a plain number (portable projects have only placeholders)."""
        self.maps, self.page, self.strict = maps, page, strict
        self.symbolize, self.forbid = symbolize, forbid
        self.own_names = own_names
        self.own_comp = own_comp if own_comp is not None else (maps.comp.get(page, {}) if page is not None else None)
        self.findings, self.edits = [], []

    # -- helpers
    def _find(self, kind, domain, text, message):
        self.findings.append(Finding(kind, domain, text, message))

    def _lit(self, domain, value, span, text, label, comp_page=None, comp_map=None):
        """Remap baseline literal `value` of `domain`; records an edit or a finding."""
        m = self.maps
        mapping = {'page': m.page, 'pic': m.pic, 'font': m.font}.get(domain) if comp_map is None else comp_map
        if domain == 'pic' and value == PIC_NONE or (domain == 'page' and value == PAGE_NONE):
            return
        if mapping is None or value not in mapping:
            return
        if self.symbolize is not None:
            repl = self.symbolize(domain, value, comp_page)
            if repl:
                self.edits.append((span[0], span[1], repl))
            return
        if self.forbid:
            self._find('newref', domain, text, 'a plain %s number %d; write ${%s:key} instead' % (
                domain, value, {'pic': 'picture', 'comp': 'obj'}.get(domain, domain)))
            return
        new = mapping[value]
        if new is None:
            self._find('dangling', domain, text, '%s %d refers to a deleted %s' % (label, value, domain))
            return
        if new == value:
            return
        if self.strict:
            self._find('newref', domain, text, 'new code uses %s number %d while %s ids change; write ${%s:key} instead' % (
                domain, value, domain, {'pic': 'picture', 'comp': 'obj'}.get(domain, domain)))
            return
        self.edits.append((span[0], span[1], str(new)))

    def _dynamic(self, domain, text, what, page=None):
        if domain == 'comp':
            if page is not None and page == self.page and self.own_comp is not None:
                relevant = not Maps._identity(self.own_comp)
            else:
                relevant = self.maps.changed('comp', page)
        else:
            relevant = self.maps.changed(domain)
        if relevant:
            self._find('dynamic', domain, text, 'computed %s reference (%s)' % (domain, what))

    def _classify(self, code, text, a, b):
        a, b = _strip(code, a, b)
        tok = code[a:b]
        if tok and set(tok) == {'\x02'}:
            return 'final', None, (a, b)
        if re.fullmatch(r'\d+', tok):
            return 'lit', int(tok), (a, b)
        return 'other', tok, (a, b)

    # -- main entry
    def run(self, text):
        code = mask(text)
        m = self.maps
        # page <arg>
        for mt in re.finditer(r'(?<![\w.])page[ \t]+([^;\n}]*)', code):
            a, b = mt.start(1), mt.end(1)
            kind, v, span = self._classify(code, text, a, b)
            if kind == 'lit':
                self._lit('page', v, span, text, 'page')
            elif kind == 'other':
                if v == 'dp':
                    pass                         # the current page id at run time is always the final id
                elif re.fullmatch(r'[A-Za-z_]\w*', v) and v in m.page_names_base.values():
                    if v not in m.page_names_final:
                        self._find('dangling', 'page', text, 'page %s was deleted or renamed' % v)
                else:
                    self._dynamic('page', text, 'page ' + v)
        # p[x].b[y], then b[y] / p[x]
        used = []
        for mt in re.finditer(r'(?<![\w.])p\s*\[([^\]]*)\]\s*\.\s*b\s*\[([^\]]*)\]', code):
            used.append(mt.span())
            pk, pv, pspan = self._classify(code, text, mt.start(1), mt.end(1))
            ck, cv, cspan = self._classify(code, text, mt.start(2), mt.end(2))
            if pk == 'lit':
                self._lit('page', pv, pspan, text, 'p[]')
            elif pk == 'other' and pv != 'dp':
                self._dynamic('page', text, 'p[%s]' % pv)
            comp_map = m.comp.get(pv) if pk == 'lit' else None
            if ck == 'lit':
                if pk == 'lit' and comp_map is not None:
                    self._lit('comp', cv, cspan, text, 'b[]', comp_page=pv, comp_map=comp_map)
                elif pk == 'final':
                    if self.strict and self.maps.changed('comp'):
                        self._find('newref', 'comp', text, 'new code uses component number %d on a placeholder page while '
                                   'component ids change; write ${obj:PAGE/OBJECT} instead' % cv)
                    elif not self.strict:
                        self._dynamic('comp', text, 'b[%d] on a page given by a placeholder' % cv)
                else:
                    self._dynamic('comp', text, 'b[%d] on a computed page' % cv)
            elif ck == 'other':
                self._dynamic('comp', text, 'b[%s]' % cv, pv if pk == 'lit' else None)
        for mt in re.finditer(r'(?<![\w.\]])b\s*\[([^\]]*)\]', code):
            if any(s <= mt.start() < e for s, e in used):
                continue
            ck, cv, cspan = self._classify(code, text, mt.start(1), mt.end(1))
            if ck == 'lit':
                if self.own_comp is not None:
                    self._lit('comp', cv, cspan, text, 'b[]', comp_page=self.page, comp_map=self.own_comp)
                else:
                    self._dynamic('comp', text, 'b[%d] without page context' % cv)
            elif ck == 'other':
                self._dynamic('comp', text, 'b[%s]' % cv, self.page)
        # <x>.pic = n  /  .font == n
        for mt in re.finditer(r'\.\s*(pic|pic1|pic2|picc|picc1|picc2|bpic|ppic|font)\s*(==|!=|<=|>=|\+=|-=|=|<|>)\s*([^\s;,)&|]*)', code):
            attr, op, rhs = mt.group(1), mt.group(2), mt.group(3)
            domain = 'font' if attr == 'font' else 'pic'
            a, b = mt.start(3), mt.end(3)
            if rhs and set(rhs) == {'\x02'}:
                continue
            nxt = code[b:].lstrip()[:1]
            if not re.fullmatch(r'\d+', rhs) or nxt in ('+', '-', '*', '/', '%', '^'):
                self._dynamic(domain, text, '.%s %s %s' % (attr, op, rhs or '...'))
            elif op in ('=', '==', '!='):
                self._lit(domain, int(rhs), (a, b), text, '.' + attr)
            elif m.changed(domain):
                self._find('ordered', domain, text, 'relative %s comparison/update on .%s' % (op, attr))
        # reversed form:  N == x.pic
        for mt in re.finditer(r'(?<![\w.])(\d+)[ \t]*(==|!=|<=|>=|<|>)[ \t]*[\w.\[\]]*\.[ \t]*(pic|pic1|pic2|picc|picc1|picc2|bpic|ppic|font)\b', code):
            domain = 'font' if mt.group(3) == 'font' else 'pic'
            if mt.group(2) in ('==', '!='):
                self._lit(domain, int(mt.group(1)), (mt.start(1), mt.end(1)), text, '.' + mt.group(3))
            elif m.changed(domain):
                self._find('ordered', domain, text, 'relative %s comparison on .%s' % (mt.group(2), mt.group(3)))
        # drawing commands
        for mt in re.finditer(r'(?m)(?:^|;)[ \t]*(pic|picq|xpic|xstr)[ \t]+([^;\n]*)', code):
            cmd = mt.group(1)
            spans = _split_args(code, mt.start(2), mt.end(2))
            idx, domain = {'pic': (2, 'pic'), 'picq': (4, 'pic'), 'xpic': (6, 'pic'), 'xstr': (4, 'font')}[cmd]
            if len(spans) <= idx:
                continue
            kind, v, span = self._classify(code, text, *spans[idx])
            if kind == 'lit':
                self._lit(domain, v, span, text, cmd)
            elif kind == 'other':
                self._dynamic(domain, text, '%s operand %s' % (cmd, v))
        # component-id commands: numeric first operand (255 = all)
        for mt in re.finditer(r'(?m)(?:^|;)[ \t]*(%s)[ \t]+([^;\n]*)' % '|'.join(ID_COMMANDS), code):
            spans = _split_args(code, mt.start(2), mt.end(2))
            kind, v, span = self._classify(code, text, *spans[0])
            if kind == 'lit' and v != 255:
                if self.own_comp is not None:
                    self._lit('comp', v, span, text, mt.group(1), comp_page=self.page, comp_map=self.own_comp)
                else:
                    self._dynamic('comp', text, '%s %d without page context' % (mt.group(1), v))
        # names that disappeared
        self._names(code, text)
        # external observers of ids
        if re.search(r'\b(?:prints|printh|print|get)\b[^;\n]*\bdp\b', code) or re.search(r'\bsendme\b', code):
            self._find('external', 'page', text, 'page id is transmitted (prints/get dp, sendme)')
        return self._apply(text)

    def _names(self, code, text):
        m = self.maps
        base_names = set(m.page_names_base.values())
        removed_pages = base_names - m.page_names_final
        final_names = self.own_names if self.own_names is not None else m.comp_names_final.get(self.page, set())
        removed_comp = set()
        if self.page is not None and self.page in m.comp_names_base:
            removed_comp = set(m.comp_names_base[self.page].values()) - final_names
        known = m.global_names | (final_names if self.page is not None else set())
        for tok in re.finditer(r'(?<![\w.])([A-Za-z_]\w*)(?:\s*\.\s*([A-Za-z_]\w*))?', code):
            first, second = tok.group(1), tok.group(2)
            if first in base_names:
                if second and first in removed_pages:
                    self._find('dangling', 'page', text, 'page %s was deleted or renamed' % first)
                elif second and first in m.page_names_final:
                    owner = next((k for k, v in m.page_names_base.items() if v == first), None)
                    gone = set(m.comp_names_base.get(owner, {}).values()) - m.comp_names_final.get(owner, set())
                    if second in gone and second not in m.global_names:
                        self._find('dangling', 'comp', text, '%s.%s was deleted or renamed' % (first, second))
            elif first in removed_comp and first not in known:
                self._find('dangling', 'comp', text, 'component %s was deleted or renamed' % first)

    def _apply(self, text):
        out, last = [], 0
        for a, b, r in sorted(set(self.edits)):
            if a < last:
                continue
            out.append(text[last:a])
            out.append(r)
            last = b
        out.append(text[last:])
        return ''.join(out)


def expand(text, resolver):
    """Replace `${kind:key}` placeholders using resolver(kind, key) -> str."""
    return PLACEHOLDER.sub(lambda mt: str(resolver(mt.group(1), mt.group(2))), text)


def attr_domain(type_code, name):
    """(domain, sentinel) for a reference-bearing attribute of a component type, else None."""
    if name in PIC_ATTRS:
        return 'pic', PIC_NONE
    if name == 'font' and type_code != 121:
        return 'font', None
    if type_code == 121 and name in PAGE_SWIPE:
        return 'page', PAGE_NONE
    if type_code == 2 and name == 'vid':
        return 'anim', PIC_NONE         # animation id; only portable projects can move animations
    return None
