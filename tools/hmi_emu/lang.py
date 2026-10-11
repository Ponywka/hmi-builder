"""Parser and evaluator of the instruction language of the display (Nextion / USART HMI dialect).

``parse(lines)`` turns the lines of an event (or of ``Program.s``) into statements; ``run(statements, env)``
executes them.  Everything that touches the display (variables, components, commands) goes through ``env``:

* ``env.read(ref)`` / ``env.write(ref, value)`` - ``ref`` is a ``Ref`` (a name path such as ``["p", 3, "b", 2, "val"]``),
* ``env.command(name, args, raw)`` - ``args`` are AST nodes (``env.value(node)`` evaluates one),
* ``env.value(node)`` evaluates an expression node.
"""

from __future__ import annotations

import re


class HmiError(Exception):
    """An error the display would report; ``code`` is the Nextion return code (0x1a invalid variable...)."""

    def __init__(self, message, code=0x00):
        super().__init__(message)
        self.code = code


class Ref:
    """A name path: ``["t0", "txt"]``, ``["page1", "n0", "val"]``, ``["p", <node>, "b", <node>, "val"]``."""

    def __init__(self, parts):
        self.parts = parts

    def __repr__(self):
        return "Ref(%r)" % (self.parts,)


class Num:
    def __init__(self, value):
        self.value = value


class Str:
    def __init__(self, value):
        self.value = value


class Bin:
    def __init__(self, op, a, b):
        self.op, self.a, self.b = op, a, b


class Un:
    def __init__(self, op, a):
        self.op, self.a = op, a


class Assign:
    def __init__(self, target, op, expr):
        self.target, self.op, self.expr = target, op, expr


class Command:
    def __init__(self, name, args, raw):
        self.name, self.args, self.raw = name, args, raw


class If:
    def __init__(self, branches, orelse):
        self.branches, self.orelse = branches, orelse      # [(cond, [stmt])], [stmt]


class While:
    def __init__(self, cond, body):
        self.cond, self.body = cond, body


class For:
    def __init__(self, init, cond, step, body):
        self.init, self.cond, self.step, self.body = init, cond, step, body


COMMANDS = {
    "prints", "printh", "print", "page", "vis", "tsw", "click", "ref", "ref_stop", "ref_star", "wepo", "repo", "wept",
    "rept", "covx", "cov", "btlen", "strlen", "substr", "spstr", "rest", "get", "sendme", "sendxy", "cls", "pic",
    "picq", "xpic", "xstr", "fill", "line", "draw", "cir", "cirs", "twfile", "delfile", "whmi-wri", "addt", "cle",
    "play", "stop", "setlayer", "com_star", "com_stop", "doevents", "randset", "rand", "refresh",
}

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>0[xX][0-9a-fA-F]+|\d+)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<id>[A-Za-z_\u0080-\uffff][\w\u0080-\uffff]*)
  | (?P<op>\+\+|--|\+=|-=|\*=|/=|%=|==|!=|<=|>=|&&|\|\||[-+*/%<>=!(),.;\[\]&|^~])
""", re.X)


def _strip_comment(line):
    out, quote, i = [], False, 0
    while i < len(line):
        c = line[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 1
            elif c == '"':
                quote = False
        elif c == '"':
            quote = True
            out.append(c)
        elif c == "/" and line[i:i + 2] == "//":
            break
        else:
            out.append(c)
        i += 1
    return "".join(out).strip()


def tokenize(text):
    pos, out = 0, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise HmiError("cannot parse %r" % text[pos:pos + 12])
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        out.append((kind, m.group()))
    return out


def unquote(token):
    body = token[1:-1]
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            out.append({"r": "\r", "n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(n, "\\" + n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class _Parser:
    """Recursive descent over the tokens of one expression / statement."""

    def __init__(self, tokens):
        self.t = tokens
        self.i = 0

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def accept(self, value):
        if self.peek()[1] == value and self.peek()[0] == "op":
            self.i += 1
            return True
        return False

    def expect(self, value):
        if not self.accept(value):
            raise HmiError("expected %r near %r" % (value, self.peek()[1]))

    def done(self):
        return self.i >= len(self.t)

    # expression, lowest precedence first
    def expr(self):
        return self.or_()

    def or_(self):
        a = self.and_()
        while self.accept("||"):
            a = Bin("||", a, self.and_())
        return a

    def and_(self):
        a = self.cmp()
        while self.accept("&&"):
            a = Bin("&&", a, self.cmp())
        return a

    def cmp(self):
        a = self.bit()
        while self.peek()[0] == "op" and self.peek()[1] in ("==", "!=", "<", ">", "<=", ">="):
            op = self.take()[1]
            a = Bin(op, a, self.bit())
        return a

    def bit(self):
        a = self.add()
        while self.peek()[0] == "op" and self.peek()[1] in ("&", "|", "^"):
            op = self.take()[1]
            a = Bin(op, a, self.add())
        return a

    def add(self):
        a = self.mul()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            a = Bin(op, a, self.mul())
        return a

    def mul(self):
        a = self.unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "%"):
            op = self.take()[1]
            a = Bin(op, a, self.unary())
        return a

    def unary(self):
        if self.peek()[0] == "op" and self.peek()[1] in ("-", "!", "~", "+"):
            op = self.take()[1]
            return Un(op, self.unary())
        return self.primary()

    def primary(self):
        kind, value = self.take()
        if kind == "num":
            return Num(int(value, 0))
        if kind == "str":
            return Str(unquote(value))
        if kind == "op" and value == "(":
            e = self.expr()
            self.expect(")")
            return e
        if kind == "id":
            return self.ref(value)
        raise HmiError("unexpected %r" % (value,))

    def ref(self, first):
        parts = [first]
        while True:
            if self.peek() == ("op", "["):
                self.take()
                parts.append(self.expr())
                self.expect("]")
            elif self.peek() == ("op", ".") and self.peek(1)[0] in ("id", "num"):
                self.take()
                parts.append(self.take()[1])
            else:
                break
        return Ref(parts)


def parse_expression(text):
    p = _Parser(tokenize(text))
    e = p.expr()
    if not p.done():
        raise HmiError("trailing tokens in %r" % text)
    return e


def _split_args(tokens):
    """Split a token list at top-level commas."""
    args, depth, cur = [], 0, []
    for tok in tokens:
        if tok == ("op", "(") or tok == ("op", "["):
            depth += 1
        elif tok == ("op", ")") or tok == ("op", "]"):
            depth -= 1
        if tok == ("op", ",") and depth == 0:
            args.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur or args:
        args.append(cur)
    return args


def _expr_of(tokens):
    p = _Parser(tokens)
    e = p.expr()
    if not p.done():
        raise HmiError("cannot parse expression")
    return e


def parse_simple(text):
    """One statement without a block: an assignment, a command, or ``int a=0,b=1``."""
    tokens = tokenize(text)
    if not tokens:
        return None
    if tokens[0] == ("id", "int") and len(tokens) > 1:
        decls = []
        for part in _split_args(tokens[1:]):
            decls.append(_assignment(part))
        return Command("int", decls, text)
    name = tokens[0][1] if tokens[0][0] == "id" else None
    if name in COMMANDS and not (len(tokens) > 1 and tokens[1][0] == "op" and tokens[1][1] in ("=", "+=", "-=", "*=", "/=", "%=", ".", "++", "--", "[")):
        if name == "printh":
            return Command(name, [], text)
        rest = tokens[1:]
        return Command(name, [_expr_of(a) for a in _split_args(rest) if a], text)
    return _assignment(tokens)


def _assignment(tokens):
    p = _Parser(tokens)
    kind, first = p.take()
    if kind != "id":
        raise HmiError("cannot parse statement")
    target = p.ref(first)
    if p.peek() == ("op", "(") and len(target.parts) >= 2 and isinstance(target.parts[-1], str):
        p.take()                                    # obj.method(args)
        inner = tokens[p.i:-1] if tokens[-1] == ("op", ")") else None
        if inner is None:
            raise HmiError("bad method call")
        return Command(".call", [Ref(target.parts[:-1]), Str(target.parts[-1])] + [_expr_of(a) for a in _split_args(inner) if a], "")
    op = p.take()[1]
    if op in ("++", "--"):
        return Assign(target, op, None)
    if op not in ("=", "+=", "-=", "*=", "/=", "%="):
        raise HmiError("unknown instruction")
    e = p.expr()
    if not p.done():
        raise HmiError("trailing tokens")
    return Assign(target, op, e)


def parse(lines):
    """Lines of code -> statements.  Raises HmiError on a syntax error."""
    items = []                  # flat list of ("{",) ("}",) ("if", cond) ("else",) ("elseif", cond) ("for", ...) ...
    for raw in lines:
        if not isinstance(raw, str):
            raw = raw.get("text", "")
        for physical in raw.split("\n"):
            line = _strip_comment(physical)
            try:
                while line:
                    line = _structural(line, items)
            except HmiError as exc:
                raise HmiError("%s [%s]" % (exc, physical.strip()), exc.code)
    out, pos = _block(items, 0, top=True)
    return out


def _structural(line, items):
    """Consume one structural piece from the start of ``line``; returns the rest."""
    if line.startswith("{"):
        items.append(("{",))
        return line[1:].strip()
    if line.startswith("}"):
        items.append(("}",))
        return line[1:].strip()
    m = re.match(r"else\s+if\s*\(", line)
    if m:
        cond, rest = _paren(line, m.end() - 1)
        items.append(("elseif", parse_expression(cond)))
        return rest.strip()
    if re.match(r"else\b", line):
        items.append(("else",))
        return line[4:].strip()
    m = re.match(r"(if|while)\s*\(", line)
    if m:
        cond, rest = _paren(line, m.end() - 1)
        items.append((m.group(1), parse_expression(cond)))
        return rest.strip()
    m = re.match(r"for\s*\(", line)
    if m:
        inner, rest = _paren(line, m.end() - 1)
        parts = inner.split(";")
        if len(parts) != 3:
            raise HmiError("bad for()")
        items.append(("for", parse_simple(parts[0]), parse_expression(parts[1]), parse_simple(parts[2])))
        return rest.strip()
    # a simple statement may be followed by '}' on the same line
    depth, quote, cut = 0, False, len(line)
    for i, c in enumerate(line):
        if c == '"' and (i == 0 or line[i - 1] != "\\"):
            quote = not quote
        elif not quote and c == "}":
            cut = i
            break
    stmt = parse_simple(line[:cut].strip())
    if stmt is not None:
        items.append(("stmt", stmt))
    return line[cut:].strip()


def _paren(line, start):
    """``line[start]`` is '('; returns (inner text, rest after the matching ')')."""
    depth, quote = 0, False
    for i in range(start, len(line)):
        c = line[i]
        if c == '"' and line[i - 1] != "\\":
            quote = not quote
        elif quote:
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                inner = line[start + 1:i]
                return inner, line[i + 1:]
    raise HmiError("unbalanced parentheses")


def _body(items, pos):
    """The body after a header: a ``{ ... }`` block or a single statement."""
    if pos < len(items) and items[pos][0] == "{":
        return _block(items, pos + 1, top=False)
    if pos < len(items):
        stmt, pos = _statement(items, pos)
        return ([stmt] if stmt is not None else []), pos
    return [], pos


def _statement(items, pos):
    it = items[pos]
    kind = it[0]
    if kind == "stmt":
        return it[1], pos + 1
    if kind == "if":
        branches = []
        body, pos = _body(items, pos + 1)
        branches.append((it[1], body))
        orelse = []
        while pos < len(items) and items[pos][0] in ("elseif", "else"):
            if items[pos][0] == "elseif":
                body, p2 = _body(items, pos + 1)
                branches.append((items[pos][1], body))
                pos = p2
            else:
                orelse, pos = _body(items, pos + 1)
                break
        return If(branches, orelse), pos
    if kind == "while":
        body, pos = _body(items, pos + 1)
        return While(it[1], body), pos
    if kind == "for":
        body, pos = _body(items, pos + 1)
        return For(it[1], it[2], it[3], body), pos
    raise HmiError("unexpected %s" % kind)


def _block(items, pos, top):
    out = []
    while pos < len(items):
        kind = items[pos][0]
        if kind == "}":
            if top:
                raise HmiError("unmatched }")
            return out, pos + 1
        if kind == "{":                      # a bare block
            body, pos = _block(items, pos + 1, top=False)
            out.extend(body)
            continue
        stmt, pos = _statement(items, pos)
        if stmt is not None:
            out.append(stmt)
    if not top:
        raise HmiError("missing }")
    return out, pos


MAX_STEPS = 200000


class Stop(Exception):
    """Raised to abandon a running event (page change inside a handler keeps going in the real display)."""


def run(statements, env, steps=None):
    steps = steps if steps is not None else [0]
    for st in statements:
        _exec(st, env, steps)


def _tick(steps):
    steps[0] += 1
    if steps[0] > MAX_STEPS:
        raise HmiError("event runs too long")


def _truth(v):
    return bool(v) if not isinstance(v, str) else bool(v)


def _exec(st, env, steps):
    _tick(steps)
    if isinstance(st, Assign):
        if st.op in ("++", "--"):
            cur = env.read(st.target)
            env.write(st.target, cur + (1 if st.op == "++" else -1))
        elif st.op == "=":
            env.write(st.target, env.value(st.expr))
        else:
            cur = env.read(st.target)
            val = env.value(st.expr)
            if st.op == "-=" and isinstance(cur, str) and not isinstance(val, str):
                env.write(st.target, cur[:max(0, len(cur) - int(val))])      # text-=N removes the last N characters
            else:
                env.write(st.target, binop(st.op[0], cur, val))
    elif isinstance(st, Command):
        env.command(st.name, st.args, st.raw)
    elif isinstance(st, If):
        for cond, body in st.branches:
            if _truth(env.value(cond)):
                run(body, env, steps)
                return
        run(st.orelse, env, steps)
    elif isinstance(st, While):
        while _truth(env.value(st.cond)):
            _tick(steps)
            run(st.body, env, steps)
    elif isinstance(st, For):
        if st.init is not None:
            _exec(st.init, env, steps)
        while _truth(env.value(st.cond)):
            _tick(steps)
            run(st.body, env, steps)
            if st.step is not None:
                _exec(st.step, env, steps)


def binop(op, a, b):
    if op == "+" and (isinstance(a, str) or isinstance(b, str)):
        return str(a) + str(b)
    if isinstance(a, str) or isinstance(b, str):
        if op in ("==", "!="):
            return int((a == b) == (op == "=="))
        raise HmiError("text and number cannot be mixed", 0x1b)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            raise HmiError("division by zero", 0x1b)
        return int(a / b)
    if op == "%":
        if b == 0:
            raise HmiError("division by zero", 0x1b)
        return a - int(a / b) * b
    if op == "==":
        return int(a == b)
    if op == "!=":
        return int(a != b)
    if op == "<":
        return int(a < b)
    if op == ">":
        return int(a > b)
    if op == "<=":
        return int(a <= b)
    if op == ">=":
        return int(a >= b)
    if op == "&":
        return a & b
    if op == "|":
        return a | b
    if op == "^":
        return a ^ b
    raise HmiError("unknown operator %s" % op)


def evaluate(node, env):
    """Evaluate an expression node (the ``env.value`` implementation of the device uses this)."""
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Ref):
        return env.read(node)
    if isinstance(node, Un):
        v = evaluate(node.a, env)
        if node.op == "-":
            return -v
        if node.op == "!":
            return int(not v)
        if node.op == "~":
            return ~v
        return v
    if isinstance(node, Bin):
        if node.op == "&&":
            return int(bool(evaluate(node.a, env)) and bool(evaluate(node.b, env)))
        if node.op == "||":
            return int(bool(evaluate(node.a, env)) or bool(evaluate(node.b, env)))
        return binop(node.op, evaluate(node.a, env), evaluate(node.b, env))
    raise HmiError("cannot evaluate")
