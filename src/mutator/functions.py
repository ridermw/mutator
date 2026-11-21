"""Bind mutation sites to the functions crapper reports."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from mutator.crapper_link import ensure_crapper
from mutator.model import Site
from mutator.sites import discover_raw

_PS_EXPORT_RE = re.compile(
    r"Export-ModuleMember\b[^\r\n;#|]*?-Function\s+([^\r\n;#|]+)",
    re.IGNORECASE,
)


def _powershell_static_exports(source: str) -> set[str] | None:
    """Static Export-ModuleMember -Function names (lowercased), or None if absent/dynamic."""

    matches = _PS_EXPORT_RE.findall(source)
    if not matches:
        return None
    exported: set[str] = set()
    for expr in matches:
        # Stop before the next cmdlet parameter (e.g. `-Alias`, `-Variable`, `-Cmdlet`).
        expr = re.split(r"\s+-[A-Za-z]", expr, maxsplit=1)[0]
        if "$" in expr or "*" in expr:
            return None
        for item in expr.strip().lstrip("@(").rstrip(")").split(","):
            cleaned = item.strip().strip("'\"")
            if cleaned:
                exported.add(cleaned.lower())
    return exported or None


def _lines(source: str) -> list[str]:
    return source.splitlines()


def _slice(source: str, start: int, end: int) -> str:
    lines = _lines(source)
    return "\n".join(lines[start - 1 : end])


def digest(source: str, start: int, end: int) -> str:
    chunk = "\n".join(line.rstrip() for line in _lines(source)[start - 1 : end])
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()


def _header(source: str, start: int, end: int) -> str:
    text = _slice(source, start, min(end, start + 6)).lstrip()
    while text.startswith("#["):
        close = text.find("]")
        if close < 0:
            break
        text = text[close + 1 :].lstrip()
    return text


def _python_private(name: str) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return False
    return name.startswith("_")


def _typescript_private(name: str, header: str) -> bool:
    return name.startswith("#") or bool(re.search(r"\bprivate\b", header))


def _declaration_prefix(
    source: str, start: int, end: int, start_byte: int = -1, end_byte: int = -1
) -> str:
    """Text before the parameter list or body (`(`, `{`, `=>`), never body strings.

    When `start_byte`/`end_byte` are present, slices the exact parsed declaration
    so same-line enclosing types or sibling methods cannot bleed into the prefix.
    """

    if start_byte >= 0 and end_byte > start_byte:
        raw = source.encode("utf-8")[start_byte:end_byte].decode("utf-8", errors="replace").lstrip()
    else:
        raw = _header(source, start, end)
    text = raw
    # Strip leading C# attributes `[Attr(...)]`
    while text.startswith("["):
        close = text.find("]")
        if close < 0:
            break
        text = text[close + 1 :].lstrip()
    for stop in ("(", "{", "=>"):
        idx = text.find(stop)
        if idx >= 0:
            text = text[:idx]
    return text


def is_private(
    language: str,
    name: str,
    source: str,
    start: int,
    end: int,
    start_byte: int = -1,
    end_byte: int = -1,
) -> bool:
    """True when uml-viewer should treat the operation as private."""

    header = _header(source, start, end)
    if language == "clojure":
        return bool(re.match(r"\(defn-", header))
    if language == "csharp":
        prefix = _declaration_prefix(source, start, end, start_byte, end_byte)
        if re.search(r"\bprivate\b", prefix):
            return True
        return not re.search(r"\b(public|protected|internal)\b", prefix)
    if language == "powershell":
        if start_byte >= 0 and end_byte > start_byte:
            sig_src = source.encode("utf-8")[start_byte:end_byte].decode("utf-8", errors="replace").lstrip()
        else:
            sig_src = _lines(source)[start - 1].lstrip() if start - 1 < len(_lines(source)) else ""
        sig = sig_src.split("{", 1)[0].split("(", 1)[0]
        if re.match(r"^hidden\b", sig, re.IGNORECASE):
            return True
        exports = _powershell_static_exports(source)
        if exports is not None and name.lower() not in exports:
            return True
        return False
    if language == "java":
        return bool(re.search(r"\bprivate\b", header))
    if language == "go":
        return name[:1].islower()
    if language == "python":
        return _python_private(name)
    if language == "rust":
        return re.match(r"pub\s", header) is None
    if language == "typescript":
        return _typescript_private(name, header)
    return False


def form_id(name: str, private: bool) -> str:
    """The id uml-viewer splits into an operation name.

    `defn/place` is public. `defn-/hide` is private. The viewer checks the
    private prefix first.
    """

    prefix = "defn-" if private else "defn"
    return f"{prefix}/{name}"


def mutation_id(
    file: str,
    namespace: str,
    form: str,
    ordinal: int,
    start: int,
    end: int,
    original: str,
    mutant: str,
) -> str:
    """`ordinal` counts functions sharing one form id, and `start`/`end` are
    bytes from the start of the function's first line, so an id survives moving
    the function vertically as its digest does."""

    return json.dumps(
        [file, namespace, form, ordinal, start, end, original, mutant], separators=(",", ":")
    )


def _mutation_field(mutation: str, index: int) -> str | None:
    try:
        value = json.loads(mutation)
    except json.JSONDecodeError:
        return None
    if isinstance(value, list) and len(value) > index and isinstance(value[index], str):
        return value[index]
    return None


def mutation_file(mutation: str) -> str | None:
    return _mutation_field(mutation, 0)


def mutation_namespace(mutation: str) -> str | None:
    return _mutation_field(mutation, 1)


def project_functions(source: str, path: Path, root: Path):
    crapper = ensure_crapper()
    language = crapper.discover.language_of(path)
    if language is None:
        return []
    return crapper.languages.functions_in_file(
        language,
        source,
        path.resolve().as_posix(),
        root.resolve().as_posix(),
    )


def _contains(fn, line: int, byte: int | None) -> bool:
    if fn.start_line > line or fn.end_line < line:
        return False
    start = getattr(fn, "start_byte", -1)
    end = getattr(fn, "end_byte", -1)
    if byte is None or start < 0 or end < 0:
        return True
    return start <= byte < end


def _tightness(fn) -> int:
    start = getattr(fn, "start_byte", -1)
    end = getattr(fn, "end_byte", -1)
    if start >= 0 and end >= start:
        return end - start
    return (fn.end_line - fn.start_line) * 1_000_000


def _owner(functions, line: int, byte: int | None = None):
    matches = [fn for fn in functions if _contains(fn, line, byte)]
    if not matches:
        return None
    return min(matches, key=lambda fn: (_tightness(fn), fn.start_line))


def _line_starts(source: str) -> list[int]:
    """Byte offset of each line, 1-indexed via starts[line - 1]."""

    starts = [0]
    for line in source.encode("utf-8").splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _ordinals(functions, privacy: dict[int, bool]) -> dict[int, int]:
    """Each function's place among those sharing its namespace and form id."""

    seen: dict[tuple[str, str], int] = {}
    ordinals = {}
    for fn in sorted(functions, key=lambda fn: (fn.start_line, fn.end_line, getattr(fn, "start_byte", -1))):
        key = (fn.namespace, form_id(fn.name, privacy[id(fn)]))
        ordinals[id(fn)] = seen.get(key, 0)
        seen[key] = ordinals[id(fn)] + 1
    return ordinals


def sites_in_file(source: str, path: Path, root: Path, file_key: str) -> list[Site]:
    crapper = ensure_crapper()
    language = crapper.discover.language_of(path)
    if language is None:
        return []
    functions = project_functions(source, path, root)
    privacy = {
        id(fn): is_private(
            language,
            fn.name,
            source,
            fn.start_line,
            fn.end_line,
            getattr(fn, "start_byte", -1),
            getattr(fn, "end_byte", -1),
        )
        for fn in functions
    }
    ordinals = _ordinals(functions, privacy)
    line_starts = _line_starts(source)
    found: list[Site] = []
    for raw in discover_raw(source, language, path.as_posix()):
        owner = _owner(functions, raw.line, raw.start)
        if owner is None:
            continue
        private = privacy[id(owner)]
        form = form_id(owner.name, private)
        base = line_starts[owner.start_line - 1]
        found.append(
            Site(
                file=file_key,
                namespace=owner.namespace,
                form_id=form,
                name=owner.name,
                line=raw.line,
                start=raw.start,
                end=raw.end,
                original=raw.original,
                mutant=raw.mutant,
                category=raw.category,
                mutation_id=mutation_id(
                    file_key,
                    owner.namespace,
                    form,
                    ordinals[id(owner)],
                    raw.start - base,
                    raw.end - base,
                    raw.original,
                    raw.mutant,
                ),
                anchor_line=raw.anchor_line,
                form_line=owner.start_line,
                form_ordinal=ordinals[id(owner)],
            )
        )
    return found


def form_digests(source: str, path: Path, root: Path) -> dict[tuple[str, str], str]:
    """Map `(namespace, form id)` to a hash of the function text."""

    crapper = ensure_crapper()
    language = crapper.discover.language_of(path)
    if language is None:
        return {}
    grouped: dict[tuple[str, str], list] = {}
    for fn in project_functions(source, path, root):
        private = is_private(
            language,
            fn.name,
            source,
            fn.start_line,
            fn.end_line,
            getattr(fn, "start_byte", -1),
            getattr(fn, "end_byte", -1),
        )
        key = (fn.namespace, form_id(fn.name, private))
        grouped.setdefault(key, []).append(fn)
    digests = {}
    for key, fns in grouped.items():
        ordered = sorted(fns, key=lambda fn: (fn.start_line, fn.end_line))
        parts = [digest(source, fn.start_line, fn.end_line) for fn in ordered]
        digests[key] = hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()
    return digests


def form_spans(source: str, path: Path, root: Path) -> dict[tuple[str, str], tuple[int, int, str, bool]]:
    """`(namespace, form id)` -> start line, end line, name, private."""

    crapper = ensure_crapper()
    language = crapper.discover.language_of(path)
    spans = {}
    for fn in project_functions(source, path, root):
        private = is_private(
            language or "",
            fn.name,
            source,
            fn.start_line,
            fn.end_line,
            getattr(fn, "start_byte", -1),
            getattr(fn, "end_byte", -1),
        )
        key = (fn.namespace, form_id(fn.name, private))
        if key not in spans:
            spans[key] = (fn.start_line, fn.end_line, fn.name, private)
            continue
        start, end, name, private_flag = spans[key]
        spans[key] = (min(start, fn.start_line), max(end, fn.end_line), name, private_flag)
    return spans


def form_declarations(
    source: str, path: Path, root: Path
) -> list[tuple[str, str, int, int, str, bool, str, int]]:
    """Every distinct declaration in source order:
    `(namespace, form_id, start, end, name, private, digest, ordinal)`."""

    crapper = ensure_crapper()
    language = crapper.discover.language_of(path) or ""
    functions = sorted(project_functions(source, path, root), key=lambda f: (f.start_line, f.end_line, getattr(f, "start_byte", -1)))
    seen: dict[tuple[str, str], int] = {}
    declarations = []
    for fn in functions:
        private = is_private(
            language,
            fn.name,
            source,
            fn.start_line,
            fn.end_line,
            getattr(fn, "start_byte", -1),
            getattr(fn, "end_byte", -1),
        )
        fid = form_id(fn.name, private)
        key = (fn.namespace, fid)
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        start_b = getattr(fn, "start_byte", -1)
        end_b = getattr(fn, "end_byte", -1)
        if start_b >= 0 and end_b > start_b:
            chunk = source.encode("utf-8")[start_b:end_b].decode("utf-8", errors="replace")
            dg = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
        else:
            dg = digest(source, fn.start_line, fn.end_line)
        declarations.append(
            (fn.namespace, fid, fn.start_line, fn.end_line, fn.name, private, dg, ordinal)
        )
    return declarations
