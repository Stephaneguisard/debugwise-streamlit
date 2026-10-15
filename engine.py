"""DebugWise engine. Runs inside Pyodide (a Web Worker in the browser).

dw_dispatch(json) -> json with three request types:
  run       execute code (and optionally evaluate a call), capture output and errors
  tests     run `call => expected` tests against code
  diagnose  static + runtime analysis of buggy code: category, misconception, 3 support levels
"""
import ast, builtins, contextlib, difflib, io, json, math, re, traceback

STD_MODULES = ["math", "random", "os", "sys", "json", "re", "time", "datetime",
               "collections", "itertools", "statistics", "string"]


# ---------------------------------------------------------------- running code
def _lines(code):
    return code.split("\n")


def _short(v, n=300):
    r = repr(v)
    return r if len(r) <= n else r[:n] + "..."


def _same(a, b):
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    try:
        return bool(a == b)
    except Exception:
        return False


def _make_input(lines, out):
    it = iter(lines)

    def _input(prompt=""):
        try:
            v = next(it)
        except StopIteration:
            raise EOFError("input() was called but no input was provided. "
                           "Type sample inputs in the Input box, one per line.")
        out.write(str(prompt) + v + "\n")
        return v
    return _input


def _err_info(e, code, call):
    src = _lines(code)
    info = {"type": type(e).__name__, "line": None, "tb": ""}
    if isinstance(e, SyntaxError):
        info["msg"] = e.msg or str(e)
        if e.filename == "<student>":
            info["line"] = e.lineno
        shown = ""
        if e.lineno and 1 <= e.lineno <= len(src):
            shown = "\n    " + src[e.lineno - 1].strip()
        info["tb"] = "%s on line %s: %s%s" % (type(e).__name__, e.lineno, info["msg"], shown)
        return info
    info["msg"] = str(e)
    frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename in ("<student>", "<call>")]
    parts = ["Traceback (most recent call last):"]
    shown = frames
    skipped = 0
    if len(frames) > 6:
        shown = frames[:2] + frames[-2:]
        skipped = len(frames) - 4
    for i, f in enumerate(shown):
        if skipped and i == 2:
            parts.append("  ... %d more frames ..." % skipped)
        if f.filename == "<student>":
            text = src[f.lineno - 1].strip() if f.lineno and f.lineno <= len(src) else ""
            parts.append("  line %s, in %s" % (f.lineno, f.name))
            parts.append("    " + text)
        else:
            parts.append("  in your call: %s" % call)
    for f in frames:
        if f.filename == "<student>":
            info["line"] = f.lineno
    parts.append("%s: %s" % (type(e).__name__, e))
    info["tb"] = "\n".join(parts)
    return info


def _exec(code, call=None, stdin=None):
    out = io.StringIO()
    res = {"ok": True, "out": "", "value": None, "has_value": False, "err": None}
    ns = {"__name__": "__main__"}
    real_input = builtins.input
    builtins.input = _make_input(stdin or [], out)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                exec(compile(code, "<student>", "exec"), ns)
                if call:
                    res["value"] = eval(compile(call, "<call>", "eval"), ns)
                    res["has_value"] = True
            except BaseException as e:
                res["ok"] = False
                res["err"] = _err_info(e, code, call)
    finally:
        builtins.input = real_input
    res["out"] = out.getvalue()[:5000]
    return res


def _pack_run(res):
    return {"ok": res["ok"], "out": res["out"], "err": res["err"], "has_value": res["has_value"],
            "value": _short(res["value"]) if res["has_value"] else None}


def _run_tests(code, tests, stdin):
    rows = []
    if not tests:
        res = _exec(code, None, stdin)
        got = "no error" if res["ok"] else "%s: %s" % (res["err"]["type"], res["err"]["msg"])
        rows.append({"call": "(run the program)", "expect": "no error", "got": got,
                     "pass": res["ok"], "out": res["out"], "err": res["err"], "bad_expect": False})
        return rows
    for t in tests:
        res = _exec(code, t["call"], stdin)
        row = {"call": t["call"], "expect": t["expect"], "out": res["out"], "err": res["err"], "bad_expect": False}
        try:
            exp = eval(t["expect"], {"__builtins__": builtins})
        except Exception:
            row.update({"got": "(expected value is not valid Python)", "pass": False, "bad_expect": True})
            rows.append(row)
            continue
        row["expect"] = _short(exp)
        if res["ok"]:
            row["got"] = _short(res["value"])
            row["pass"] = _same(res["value"], exp)
        else:
            row["got"] = "%s: %s" % (res["err"]["type"], res["err"]["msg"])
            row["pass"] = False
        rows.append(row)
    return rows


# ---------------------------------------------------------------- diagnosis helpers
def _seg(code, node):
    return ast.get_source_segment(code, node) or ""


def _is_len(n):
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "len"
            and len(n.args) == 1)


def _fix_line(code, lineno, old, new):
    line = _lines(code)[lineno - 1]
    return line.replace(old, new, 1).strip()


def _r(cat, mis, conf, line, l1, l2, l3, kws):
    return {"category": cat, "misconception": mis, "confidence": conf, "line": line,
            "l1": l1, "l2": l2, "l3": l3, "keywords": kws}


def _defined_names(tree):
    names = set(dir(builtins))
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            names.add(n.name)
            if not isinstance(n, ast.ClassDef):
                for a in n.args.args + n.args.kwonlyargs:
                    names.add(a.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def _find_offbyone(tree, code):
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.iter, ast.Call)
                and isinstance(node.iter.func, ast.Name) and node.iter.func.id == "range" and node.iter.args):
            a = node.iter.args
            stop = a[1] if len(a) >= 2 else a[0]
            if (isinstance(stop, ast.BinOp) and isinstance(stop.op, ast.Add) and _is_len(stop.left)
                    and isinstance(stop.right, ast.Constant) and stop.right.value == 1):
                return {"line": node.lineno, "kind": "range", "seq": _seg(code, stop.left.args[0]),
                        "var": _seg(code, node.target),
                        "fix": _fix_line(code, node.lineno, _seg(code, stop), _seg(code, stop.left))}
        if (isinstance(node, ast.While) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.LtE) and _is_len(node.test.comparators[0])):
            seg = _seg(code, node.test)
            return {"line": node.lineno, "kind": "while", "seq": _seg(code, node.test.comparators[0].args[0]),
                    "var": _seg(code, node.test.left), "fix": _fix_line(code, node.lineno, seg, seg.replace("<=", "<", 1))}
        if isinstance(node, ast.Subscript) and _is_len(node.slice):
            seg = _seg(code, node.slice)
            return {"line": node.lineno, "kind": "subscript", "seq": _seg(code, node.slice.args[0]), "var": "",
                    "fix": _fix_line(code, node.lineno, "[" + seg + "]", "[" + seg + " - 1]")}
    return None


# ---------------------------------------------------------------- error rules
def _syntax(c):
    e = c["err"]; m = e["msg"]; t = e["type"]; ln = e.get("line")
    src = _lines(c["code"])
    line = src[ln - 1] if ln and ln <= len(src) else ""
    l1 = "Python stopped before running any line. Which line does the message point at, and what does Python expect to see at that spot?"
    if "expected ':'" in m:
        return _r("Syntax: missing colon", "Believing a block header (if / for / while / def) can end without a colon.", 0.95, ln,
                  l1, "Look at the end of line %s. Compare it with a correct if / for / def line in your notes. What character does each of them end with?" % ln,
                  "Line %s -> %s" % (ln, line.rstrip().strip() + ":"),
                  ["colon", ":", "header", "end of the line", "if", "for", "while", "def"])
    if "was never closed" in m or "unmatched" in m or "closing parenthesis" in m:
        return _r("Syntax: unbalanced bracket", "Opening a bracket or quote and forgetting to close it, so Python keeps reading into the next line.", 0.9, ln,
                  l1, "Count the opening and closing brackets on line %s and the lines just above it. Do they match?" % ln,
                  "Add the missing closing ) ] or } on or just after line %s." % ln,
                  ["bracket", "parenthes", "close", "closing", "never closed", "(", ")", "unmatched"])
    if "unterminated" in m:
        return _r("Syntax: unterminated string", "Starting a string with a quote and not ending it with the same quote on that line.", 0.9, ln,
                  l1, "Look at the quotes on line %s. Does every opening quote have a matching closing quote of the same kind?" % ln,
                  "Add the missing closing quote at the end of the string on line %s." % ln,
                  ["quote", "string", "unterminated", "close", "closing", "\"", "'"])
    if "Maybe you meant '=='" in m or ("invalid syntax" in m and re.search(r"\b(if|while|elif)\b[^=]*[^=!<>]=[^=]", line)):
        fixed = re.sub(r"(?<![=!<>])=(?!=)", "==", line, count=1)
        return _r("Syntax: = used where == is needed", "Mixing up = (store a value) with == (compare two values).", 0.92, ln,
                  l1, "On line %s you are asking a yes/no question. Which symbol asks 'are these two equal?' and which one stores a value?" % ln,
                  "Line %s -> %s" % (ln, fixed.strip()),
                  ["==", "=", "equal", "compare", "comparison", "assign", "condition", "if"])
    if t in ("IndentationError", "TabError") or "indent" in m:
        return _r("Syntax: indentation", "Believing Python ignores the spaces at the start of a line. In Python they define which lines belong to a block.", 0.9, ln,
                  l1, "Compare the indentation of line %s with the line above it. Which lines should be inside the block, and which should not?" % ln,
                  "Line %s -> indent it to match the block it belongs to (use 4 spaces consistently)." % ln,
                  ["indent", "indentation", "space", "block", "tab", "align"])
    return _r("Syntax error", "Writing something Python cannot parse.", 0.7, ln, l1,
              "Read line %s slowly, one symbol at a time. Compare it with a similar line that works in your notes." % ln,
              "Line %s -> compare it character by character with a working example; the message was: %s" % (ln, m),
              ["syntax", "line", "symbol", "typo"])


def _index(c):
    e = c["err"]; ln = e.get("line")
    f = _find_offbyone(c["tree"], c["code"]) if c["tree"] is not None else None
    kws = ["range", "len", "index", "last", "one too", "off by one", "off-by-one", "+ 1", "+1", "<=",
           "out of range", "bound", "length", "past the end", "beyond", "too far", "too many"]
    if f:
        seq, var = f["seq"], f["var"] or "the index"
        if f["kind"] == "subscript":
            l1 = "If %s has 3 items, what is its last valid index? Which index does line %s ask for?" % (seq, f["line"])
            l2 = "Focus on line %s. Print len(%s) and the index you use just before that line, then compare." % (f["line"], seq)
        else:
            l1 = "If %s has 3 items, which values does %s take in the loop? Which of those are valid indexes?" % (seq, var)
            l2 = ("Focus on line %s. In your revision, add print(%s) as the first line inside the loop and run it. "
                  "What is the last value printed before the error on line %s?" % (f["line"], var, ln))
        return _r("Off-by-one / index out of range",
                  "Treating len(%s) as the last valid index, when the last index is len(%s) - 1." % (seq, seq), 0.92, f["line"],
                  l1, l2, "Line %s -> %s" % (f["line"], f["fix"]), kws)
    return _r("Index out of range", "Using an index that is not in the list or string at that moment.", 0.7, ln,
              "A sequence with N items has valid indexes 0 to N-1. Which index does line %s use, and how long is the sequence at that moment?" % ln,
              "Print the index and the length of the sequence on the line just before line %s. Which is bigger?" % ln,
              None, kws + ["empty", "list"])


def _name_error(c):
    e = c["err"]; ln = e.get("line"); m = re.search(r"name '(\w+)' is not defined", e["msg"])
    if not m or c["tree"] is None:
        return None
    name = m.group(1); code = c["code"]
    defined = _defined_names(c["tree"])
    close = difflib.get_close_matches(name, [d for d in defined if not d.startswith("__")], n=1, cutoff=0.75)
    kws = [name, "typo", "spell", "defined", "name", "variable", "scope", "import", "exists"]
    if name in STD_MODULES:
        return _r("Missing import", "Believing standard modules are available without importing them.", 0.92, ln,
                  "The name %s is not defined. Where in your program does it get created or imported?" % name,
                  "Python only knows the names you create or import. Look at the top of your file: is %s there?" % name,
                  "Add at the top of the file: import %s" % name, kws + ["import"])
    if close and ln:
        fixed = re.sub(r"\b%s\b" % re.escape(name), close[0], _lines(code)[ln - 1]).strip()
        return _r("Typo in a name", "Assuming Python will match a name that is spelled slightly differently from the one you created.", 0.9, ln,
                  "Python says it has never seen %s. Which names exist in your program that look similar?" % name,
                  "Compare the spelling of %s on line %s with the names you created earlier, letter by letter." % (name, ln),
                  "Line %s -> %s" % (ln, fixed), kws + ["spelling", "misspell", "capital", "case"])
    inside_func = False
    for n in ast.walk(c["tree"]):
        if isinstance(n, ast.FunctionDef):
            for x in ast.walk(n):
                if isinstance(x, ast.Name) and x.id == name and isinstance(x.ctx, ast.Store):
                    inside_func = True
    if inside_func:
        return _r("Variable scope", "Believing a variable created inside a function can be used outside it.", 0.82, ln,
                  "Where is %s created, and which part of the program can see it there?" % name,
                  "A variable made inside a function disappears when the function ends. Look at line %s: is it inside or outside that function?" % ln,
                  "Return %s from the function and store the result where you need it, or create it before line %s." % (name, ln),
                  kws + ["function", "inside", "outside", "local", "return"])
    return _r("Name used before it exists", "Using a name that was never created.", 0.7, ln,
              "On which line is %s first given a value? Does that line run before line %s?" % (name, ln),
              "Search your program for %s. Is it ever assigned, imported or passed in as a parameter?" % name,
              None, kws)


def _unbound(c):
    e = c["err"]; ln = e.get("line"); m = re.search(r"variable '(\w+)'", e["msg"])
    name = m.group(1) if m else "the variable"
    return _r("Scope: assigning inside a function", "Believing that assigning to a variable inside a function changes the outer variable of the same name.", 0.85, ln,
              "Line %s reads %s, but %s is also assigned in this function. Which %s does Python use, and does it have a value yet?" % (ln, name, name, name),
              "In Python, assigning to a name inside a function makes it local to that function. Trace what %s holds when line %s starts." % (name, ln),
              "Pass %s in as a parameter and return the new value (or, less ideal, add 'global %s' as the first line of the function)." % (name, name),
              [name, "scope", "local", "global", "function", "assign", "before", "inside"])


def _type_error(c):
    e = c["err"]; ln = e.get("line"); m = e["msg"]; code = c["code"]; tree = c["tree"]
    if "NoneType" in m:
        nores = []
        if tree is not None:
            for n in ast.walk(tree):
                if isinstance(n, ast.FunctionDef):
                    has = any(isinstance(x, ast.Return) and x.value is not None for x in ast.walk(n))
                    if not has:
                        nores.append(n.name)
        kws = ["none", "return", "returns", "print", "value", "nothing", "missing", "function"]
        if nores:
            return _r("Missing return value", "Believing a function hands back its result without a return statement; without one it returns None.", 0.82, ln,
                      "Line %s is using a value that is None. Which function produced it, and what does that function return?" % ln,
                      "Look at %s() and find its return statement. Does every path through it return a value?" % nores[0],
                      "Add a return statement with the result at the end of %s() (check it is indented inside the function)." % nores[0], kws)
        return _r("Unexpected None value", "Assuming a variable holds data when it holds None.", 0.6, ln,
                  "Line %s is using None as if it were data. Where could that value have come from?" % ln,
                  "Print the variable on the line before line %s, then follow it back to where it was last assigned." % ln, None, kws)
    if "unsupported operand type" in m or "can only concatenate" in m or "must be str" in m or "can't multiply" in m:
        fix = None
        if tree is not None and ln:
            for n in ast.walk(tree):
                if getattr(n, "lineno", None) != ln:
                    continue
                left = right = None
                if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                    left, right = n.left, n.right
                elif isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Add):
                    left, right = n.target, n.value
                if right is None:
                    continue
                pair = re.search(r"for \+=?: '(\w+)' and '(\w+)'", m)
                if pair:
                    target = right if pair.group(2) == "str" else left if pair.group(1) == "str" else None
                    conv = "int" if "str" in pair.groups() else None
                    if target is not None and conv:
                        fix = "Line %s -> %s" % (ln, _fix_line(code, ln, _seg(code, target), "int(%s)" % _seg(code, target)))
                elif "can only concatenate str" in m:
                    fix = "Line %s -> convert the number with str(...) before joining, or convert the text with int(...) before adding." % ln
                break
        return _r("Type mismatch in an operation", "Assuming values from input() or a form are numbers. Python will not mix text and numbers with +.", 0.88, ln,
                  "What is the type of each value in the operation on line %s? What does Python do when one side is text and the other is a number?" % ln,
                  "Try type(...) on both sides of the operation on line %s (print it). Then ask: which side must be converted, and with which function?" % ln,
                  fix or "Convert one side so both have the same type: int(...) / float(...) for numbers, str(...) for text.",
                  ["type", "str", "int", "string", "number", "text", "convert", "int(", "float(", "mix", "concatenat", "+", "form", "input"])
    if "positional argument" in m or "required positional" in m or "keyword argument" in m:
        return _r("Wrong number of arguments", "Calling a function with a different number of values than it was defined to take.", 0.9, ln,
                  "How many parameters does the function define, and how many values does the call on line %s give it?" % ln,
                  "Put the def line and the call on line %s side by side and match them up one by one." % ln,
                  "Make the call on line %s pass exactly the values the def line lists, in that order." % ln,
                  ["argument", "parameter", "call", "def", "number", "how many", "positional", "missing"])
    if "does not support item assignment" in m:
        return _r("Strings are immutable", "Believing you can change one character of a string in place, like an element of a list.", 0.9, ln,
                  "What type is the object on line %s? Which types in Python can be changed in place, and which cannot?" % ln,
                  "Strings cannot be edited in place. Think about building a new string, or converting to a list first.",
                  "Build a new string (e.g. s = s[:i] + new + s[i+1:]) or use list(s), change it, then ''.join(...).",
                  ["string", "immutable", "item assignment", "change", "list", "type"])
    return _r("Type error", "Using a value in a way its type does not allow.", 0.55, ln,
              "Read the last line of the error. Which type does it name, and what did you try to do with it?",
              "Print type(...) of each value used on line %s. Which one is not what you expected?" % ln, None,
              ["type", "str", "int", "list", "none", "convert", "not"])


def _zero_div(c):
    e = c["err"]; ln = e.get("line"); den = None
    if c["tree"] is not None and ln:
        for n in ast.walk(c["tree"]):
            if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Div, ast.FloorDiv, ast.Mod)) and getattr(n, "lineno", None) == ln:
                den = _seg(c["code"], n.right)
                break
    d = den or "the denominator"
    return _r("Division by zero", "Assuming the number you divide by is never zero (for example, when a list is empty).", 0.88, ln,
              "What is the value of %s when line %s runs? Could it ever be 0?" % (d, ln),
              "Print %s just before line %s, then try the smallest and emptiest input you can think of." % (d, ln),
              "Before line %s add a guard: if %s == 0: return 0  (or whatever result makes sense for an empty case)." % (ln, d),
              ["zero", "divide", "division", "denominator", "empty", "/", "len", "0"])


def _key_error(c):
    e = c["err"]; ln = e.get("line")
    return _r("Missing dictionary key", "Assuming a key exists in the dictionary before it was added.", 0.8, ln,
              "Which key does line %s ask for, and which keys does the dictionary actually have at that moment?" % ln,
              "Print the dictionary (or its keys) just before line %s and compare the key you ask for, including spelling and type." % ln,
              "Check first with: if key in the_dict: ... or use the_dict.get(key, default) on line %s." % ln,
              ["key", "dictionary", "dict", "exist", "missing", "get", "in"])


def _attr_error(c):
    e = c["err"]; ln = e.get("line"); m = re.search(r"'(\w+)' object has no attribute '(\w+)'", e["msg"])
    kws = ["method", "attribute", "name", "spelling", "dir", "type", "has no"]
    if m:
        typ, attr = m.groups()
        alt = {("list", "push"): "append", ("list", "add"): "append", ("list", "length"): "len(...)", ("str", "length"): "len(...)",
               ("list", "size"): "len(...)", ("str", "toUpperCase"): "upper", ("str", "toLowerCase"): "lower", ("str", "append"): "+ (string joining)"}.get((typ, attr))
        if typ == "NoneType":
            return _r("Missing return value", "Believing a function returns its result without a return statement; without one it returns None.", 0.75, ln,
                      "Line %s calls .%s on None. Which value is None, and which function made it?" % (ln, attr),
                      "Find the function that produced the value. Does every path through it end with return <value>?", None,
                      kws + ["none", "return"])
        if alt:
            return _r("Method from another language", "Assuming a method name from another language exists on a Python %s." % typ, 0.88, ln,
                      "Which methods does a Python %s really have? How could you list them?" % typ,
                      "Run dir(...) on a %s in an experiment and look for a method that does what .%s was meant to do." % (typ, attr),
                      "Line %s -> use %s instead of %s" % (ln, alt, attr), kws + ["other language", "javascript", "java"])
        return _r("Wrong method or attribute", "Calling a method the %s type does not have." % typ, 0.7, ln,
                  "Which methods does a %s have? How could you list them in an experiment?" % typ,
                  "Run dir(...) on a %s and look for the name closest to .%s, checking spelling and capitals." % (typ, attr), None, kws)
    return _r("Wrong method or attribute", "Using a name the object does not have.", 0.6, ln,
              "Which object is on the left of the dot on line %s, and what type is it?" % ln,
              "Print type(...) of that object and run dir(...) on it.", None, kws)


def _value_error(c):
    e = c["err"]; ln = e.get("line"); m = e["msg"]
    if "invalid literal" in m or "could not convert" in m:
        return _r("Converting text that is not a number", "Assuming the text always looks like a number before converting it.", 0.82, ln,
                  "What exact text is being converted on line %s? Is every character something int() or float() accepts?" % ln,
                  "Print the value (with repr(...) so spaces show) just before line %s. Is there a space, a letter, or an empty string?" % ln,
                  "Clean the text first (for example value.strip()), or check value.isdigit() before converting on line %s." % ln,
                  ["convert", "int", "float", "text", "string", "number", "space", "empty", "digit", "valid"])
    return _r("Value error", "Passing a value of the right type but an unacceptable content.", 0.55, ln,
              "Read the message. What value does it complain about, and what did the function expect instead?",
              "Print that value just before line %s and compare with what the function accepts." % ln, None,
              ["value", "valid", "expect", "accept", "range", "empty"])


def _err_rules(c):
    e = c["err"]; t = e["type"]
    if t in ("SyntaxError", "IndentationError", "TabError"):
        return _syntax(c)
    if t == "IndexError":
        return _index(c)
    if t == "NameError":
        return _name_error(c)
    if t == "UnboundLocalError":
        return _unbound(c)
    if t == "TypeError":
        return _type_error(c)
    if t == "ZeroDivisionError":
        return _zero_div(c)
    if t == "KeyError":
        return _key_error(c)
    if t == "AttributeError":
        return _attr_error(c)
    if t == "ValueError":
        return _value_error(c)
    if t == "RecursionError":
        return _r("Missing base case", "Believing a recursive function will stop on its own without a case that returns directly.", 0.85, e.get("line"),
                  "What input makes this function stop calling itself? Does every call move closer to that input?",
                  "Trace the function by hand for a small input, writing down the argument at each call. Does it ever reach a stopping case?",
                  "Add a base case at the top of the function that returns directly for the smallest input, and make sure the recursive call changes the argument.",
                  ["base case", "recursion", "recursive", "stop", "infinite", "itself", "terminate"])
    if t == "EOFError":
        return _r("input() with no data", "Expecting a person to type while the program runs in a page that has no keyboard prompt.", 0.9, e.get("line"),
                  "How many times does the program call input()? How many lines did you provide?",
                  "Type one sample input per line in the Input box, as many as the program asks for.",
                  "Add the missing sample inputs in the Input box.", ["input", "line", "eof", "data", "box"])
    if t in ("ModuleNotFoundError", "ImportError"):
        return _r("Module not available", "Assuming every package is installed in the browser version of Python.", 0.8, e.get("line"),
                  "Which module is being imported, and is it part of Python's standard library?",
                  "This site runs Python in your browser, so only the standard library and a few packages are available.", None,
                  ["module", "import", "install", "package", "library"])
    return None


# ---------------------------------------------------------------- logic rules (no exception)
def _body_updates(stmts, skip, name):
    for st in stmts:
        if st is skip:
            continue
        for n in ast.walk(st):
            if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
                return True
            if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
                    and any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(n.value))):
                return True
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == name and n.func.attr in ("append", "add", "extend")):
                return True
    return False


def _accum_reset(c):
    tree, code = c["tree"], c["code"]
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.While)):
            continue
        for st in loop.body:
            if (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                    and isinstance(st.value, (ast.Constant, ast.List, ast.Dict))):
                name = st.targets[0].id
                if _body_updates(loop.body, st, name):
                    return _r("Accumulator re-initialised inside the loop",
                              "Believing a variable keeps its value between passes even though the line that creates it runs again on every pass.", 0.9, st.lineno,
                              "Where does %s get its starting value, and how many times does that line run? Compare with how the other variables are set up." % name,
                              "Trace %s for the first two items on paper (write its value after each pass). Then print(%s) at the end of each pass in your revision and compare." % (name, name),
                              "Line %s -> delete it from inside the loop.\nAbove the loop (before line %s) -> add: %s" % (st.lineno, loop.lineno, _seg(code, st)),
                              [name, "reset", "resets", "inside the loop", "each time", "every time", "every pass", "start", "initial", "accumulator", "overwrit", "re-", "0", "loop"])
    return None


def _ends_return(stmts):
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, ast.Return):
        return True
    if isinstance(last, ast.If):
        return _ends_return(last.body) and _ends_return(last.orelse)
    return False


def _early_return(c):
    tree, code = c["tree"], c["code"]
    for loop in ast.walk(tree):
        if isinstance(loop, (ast.For, ast.While)) and _ends_return(loop.body):
            last = loop.body[-1]
            end = getattr(loop, "end_lineno", loop.lineno)
            if isinstance(last, ast.If) and last.orelse:
                first_else = last.orelse[0].lineno
                ret = _seg(code, last.orelse[-1]) if isinstance(last.orelse[-1], ast.Return) else "return ..."
                fix = ("Lines %s-%s -> remove the else branch.\nAfter the loop (same indentation as 'for' on line %s) -> add: %s"
                       % (max(first_else - 1, loop.lineno), end, loop.lineno, ret))
            else:
                fix = "Line %s -> the return runs on the first pass. Make it conditional, and move any 'default' return to after the loop (same indentation as 'for' on line %s)." % (last.lineno, loop.lineno)
            return _r("Return inside the loop ends the whole function",
                      "Believing return only finishes the current pass of the loop. It ends the entire function immediately.", 0.88, loop.lineno,
                      "How many items does the loop look at before the function returns? Which line makes it stop?",
                      "Trace the function by hand on a list of 3 items. At which pass does a return run? Put print() before each return to see it.",
                      fix,
                      ["return", "first", "once", "one item", "only", "stops", "ends", "loop", "exit", "immediately", "whole function", "else"])
    return None


def _short_range(c):
    tree, code = c["tree"], c["code"]
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name)
                and node.iter.func.id == "range" and node.iter.args):
            a = node.iter.args
            stop = a[1] if len(a) >= 2 else a[0]
            if (isinstance(stop, ast.BinOp) and isinstance(stop.op, ast.Sub) and _is_len(stop.left)
                    and isinstance(stop.right, ast.Constant) and stop.right.value == 1):
                seq = _seg(code, stop.left.args[0])
                used = any(isinstance(x, ast.Subscript) and _seg(code, x.value) == seq for x in ast.walk(node))
                if used:
                    return _r("Off-by-one: loop stops one item early", "Subtracting 1 from len() when range() already stops before its end value.", 0.85, node.lineno,
                              "If %s has 3 items, which indexes does this loop visit? Which item is never visited?" % seq,
                              "Print the loop variable on each pass for a 3-item list. Which item is missing from the output?",
                              "Line %s -> %s" % (node.lineno, _fix_line(code, node.lineno, _seg(code, stop), _seg(code, stop.left))),
                              ["range", "len", "last", "one", "-1", "- 1", "skip", "miss", "stop", "early", "index"])
    return None


def _floor_div(c):
    for fn in ast.walk(c["tree"]):
        if isinstance(fn, ast.FunctionDef) and re.search(r"avg|average|mean|ratio|percent|rate", fn.name, re.I):
            for n in ast.walk(fn):
                if isinstance(n, ast.BinOp) and isinstance(n.op, ast.FloorDiv):
                    seg = _seg(c["code"], n)
                    return _r("Floor division where true division is needed", "Believing // is just another way to write division. It rounds down.", 0.8, n.lineno,
                              "What does // do to the result? Compare 7 // 2 and 7 / 2 in an experiment.",
                              "Run the failing call and look at whether the answer is a whole number when it should have decimals.",
                              "Line %s -> %s" % (n.lineno, _fix_line(c["code"], n.lineno, seg, seg.replace("//", "/", 1))),
                              ["//", "floor", "round", "integer", "decimal", "division", "/"])
    return None


def _mutable_default(c):
    for fn in ast.walk(c["tree"]):
        if isinstance(fn, ast.FunctionDef):
            for d in fn.args.defaults + fn.args.kw_defaults:
                if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                    return _r("Mutable default argument", "Believing a default value like [] is created fresh on every call. It is created once, when the function is defined.", 0.85, fn.lineno,
                              "When is the default value of the parameter created: each call, or once? Call the function twice and compare.",
                              "Call the function two times in a row and print the result of each call. What do you notice about the second one?",
                              "Use None as the default, then inside the function: if items is None: items = []",
                              ["default", "mutable", "list", "[]", "shared", "same list", "once", "remembers", "previous call"])
    return None


def _is_literal(c):
    for n in ast.walk(c["tree"]):
        if isinstance(n, ast.Compare):
            for op, comp in zip(n.ops, n.comparators):
                if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(comp, ast.Constant) and isinstance(comp.value, (str, int, float)) and not isinstance(comp.value, bool):
                    seg = _seg(c["code"], n)
                    new = seg.replace(" is not ", " != ", 1).replace(" is ", " == ", 1)
                    return _r("'is' used to compare values", "Believing 'is' and '==' both ask 'are these equal?'. 'is' asks whether they are the same object.", 0.8, n.lineno,
                              "What is the difference between asking 'same value?' and 'same object?' in Python?",
                              "Try a comparison with is and with == on two equal strings built in different ways.",
                              "Line %s -> %s" % (n.lineno, _fix_line(c["code"], n.lineno, seg, new)),
                              ["is", "==", "equal", "same object", "identity", "compare"])
    return None


def _logic_rules(c):
    for rule in (_accum_reset, _early_return, _short_range, _floor_div, _mutable_default, _is_literal):
        r = rule(c)
        if r:
            return r
    return None


def _fallback(c):
    e = c["err"]
    if e:
        ln = e.get("line")
        return _r("%s (no specific pattern recognised)" % e["type"], "Not enough evidence to name the misconception.", 0.4, ln,
                  "Read the last line of the error. Which value does it name, and where did that value come from?",
                  "Add print(...) on the lines just before line %s to show the values it uses, and compare each one with your expectation." % ln,
                  None, ["error", "value", "line", "print", "variable"])
    return _r("Wrong output (no specific pattern recognised)", "Not enough evidence to name the misconception.", 0.4, None,
              "Compare the output you got with the output you expected. Which part is different, and which variable holds that part?",
              "Trace the function by hand for the smallest failing input, then print every variable that changes and find the first place where it differs from your trace.",
              None, ["output", "wrong", "expected", "variable", "print", "trace"])


def diagnose(code, failure):
    err = (failure or {}).get("err")
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None
    ctx = {"code": code, "tree": tree, "err": err, "failure": failure}
    res = None
    if err:
        res = _err_rules(ctx)
    if res is None and tree is not None:
        res = _logic_rules(ctx)
    if res is None:
        res = _fallback(ctx)
    if not res.get("l3"):
        res["l3"] = ("No single line change can be named with enough confidence. Use your prints to find the first place "
                     "where a value differs from your hand trace; the fix belongs there.")
        res["confidence"] = min(res["confidence"], 0.55)
    return res


# ---------------------------------------------------------------- entry point
def dw_dispatch(s):
    req = json.loads(s)
    t = req["type"]
    try:
        if t == "run":
            return json.dumps(_pack_run(_exec(req["code"], req.get("call") or None, req.get("stdin"))))
        if t == "tests":
            return json.dumps({"rows": _run_tests(req["code"], req.get("tests") or [], req.get("stdin"))})
        if t == "diagnose":
            return json.dumps(diagnose(req["code"], req.get("failure")))
        return json.dumps({"internal": True, "error": "unknown request"})
    except BaseException as e:
        return json.dumps({"internal": True, "error": "%s: %s" % (type(e).__name__, e)})
