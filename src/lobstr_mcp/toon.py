"""TOON (Token-Oriented Object Notation) encoder.

A compact, LLM-friendly serialization of the JSON data model: YAML-style
indentation for objects, and a header-once CSV form for arrays of uniform
objects. The win over JSON on our tool output is the tabular arrays — a page of
scraped rows declares its columns *once* instead of repeating every key on every
row.

This is a focused encoder for the shapes our MCP tools return (objects of
scalars, arrays of scalars, and arrays of flat records). Anything it can't
render tabularly falls back to inline JSON so it never loses data.
"""
from __future__ import annotations

import json
import re

_INDENT = "  "
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")
# characters that would make a bare scalar ambiguous with TOON structure
_KEY_UNSAFE = set(':,{}[]"\n\t\r')
_LEAD_UNSAFE = set('[{#-&*!|>%@`"\'')


def to_toon(obj) -> str:
    """Encode a JSON-compatible object as a TOON document."""
    lines: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            _emit_kv(str(key), value, 0, lines)
    elif isinstance(obj, list):
        _emit_array("items", obj, 0, lines)
    else:
        lines.append(_scalar(obj))
    return "\n".join(lines)


def _emit_kv(key: str, value, depth: int, lines: list[str]) -> None:
    pad = _INDENT * depth
    if isinstance(value, dict):
        lines.append(f"{pad}{_key(key)}:")
        for k2, v2 in value.items():
            _emit_kv(str(k2), v2, depth + 1, lines)
    elif isinstance(value, list):
        _emit_array(key, value, depth, lines)
    else:
        lines.append(f"{pad}{_key(key)}: {_scalar(value)}")


def _emit_array(key: str, arr: list, depth: int, lines: list[str]) -> None:
    pad = _INDENT * depth
    n = len(arr)
    if n == 0:
        lines.append(f"{pad}{_key(key)}[0]:")
        return

    # array of scalars -> inline
    if all(not isinstance(x, (dict, list)) for x in arr):
        cells = ",".join(_cell(x) for x in arr)
        lines.append(f"{pad}{_key(key)}[{n}]: {cells}")
        return

    # array of flat records -> tabular (columns declared once)
    if all(isinstance(x, dict) for x in arr):
        cols: list[str] = []
        seen: set[str] = set()
        for row in arr:
            for k in row:
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
        flat = all(not isinstance(row.get(c), (dict, list))
                   for row in arr for c in cols)
        if flat:
            header = ",".join(_key(c) for c in cols)
            lines.append(f"{pad}{_key(key)}[{n}]{{{header}}}:")
            for row in arr:
                lines.append(f"{pad}{_INDENT}" + ",".join(_cell(row.get(c)) for c in cols))
            return

    # anything more complex: keep the data, fall back to inline JSON
    lines.append(f"{pad}{_key(key)}[{n}]: {json.dumps(arr, ensure_ascii=False)}")


def _num(v) -> str:
    if isinstance(v, bool):  # bool is an int subclass; guard first
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


def _scalar(v) -> str:
    """A value in object position (after ``key: ``)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _num(v)
    return _quote(str(v)) if _needs_quote(str(v), in_cell=False) else str(v)


def _cell(v) -> str:
    """A value in a CSV cell (comma-delimited; null renders empty)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _num(v)
    s = str(v)
    return _quote(s) if _needs_quote(s, in_cell=True) else s


def _needs_quote(s: str, in_cell: bool) -> bool:
    if s == "":
        return in_cell is False  # empty cell is fine bare; empty value needs ""
    if s != s.strip():
        return True
    if any(c in s for c in '"\n\t\r'):
        return True
    if ":" in s:
        return True
    if in_cell and "," in s:
        return True
    if s in ("true", "false", "null") or _NUM_RE.match(s):
        return True
    return s[0] in _LEAD_UNSAFE


def _quote(s: str) -> str:
    s = (s.replace("\\", "\\\\").replace('"', '\\"')
          .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r"))
    return f'"{s}"'


def _key(k: str) -> str:
    if k == "" or k != k.strip() or any(c in _KEY_UNSAFE for c in k):
        return _quote(k)
    return k
