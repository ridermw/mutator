"""Read and write `.metrics/mutate` snapshots.

uml-viewer loads every `*.edn` file under `.metrics/mutate`. Each file is one
namespace. It joins a form to an operation when the form id is `defn/<name>`
or `defn-/<name>`, and it joins the file to a class on `:namespace`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from mutator.edn import dumps, keyword, loads
from mutator.functions import mutation_file, mutation_namespace
from mutator.model import FormResult, History, PriorForm


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def source_key(path: Path, root: Path) -> str:
    return _relative(path, root)


def snapshot_path(root: Path, namespace: str) -> Path:
    parts = namespace.replace("\\", "/").replace("::", "/").split("/")
    safe = []
    for part in parts:
        if part in {"", ".", ".."}:
            raise ValueError(f"unsafe namespace {namespace!r}")
        safe.append(part)
    return root.joinpath(".metrics", "mutate", *safe[:-1], safe[-1] + ".edn")


def _edn_files(root: Path) -> list[Path]:
    directory = root / ".metrics" / "mutate"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*.edn") if path.is_file())


def _read(path: Path) -> dict | None:
    try:
        data = loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _forms_of(data: dict) -> list[dict]:
    forms = data.get("forms")
    if not isinstance(forms, list):
        return []
    return [form for form in forms if isinstance(form, dict)]


_RECORDABLE_OUTCOMES = {"killed", "survived", "invalid", "error"}
CLASSIFIER_VERSION = 2


def _outcomes_of(data: dict) -> dict[str, str]:
    raw = data.get("outcomes")
    if not isinstance(raw, dict):
        return {}
    found = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        status = value[1:] if isinstance(value, str) and value.startswith(":") else value
        if status in _RECORDABLE_OUTCOMES:
            found[key] = status
    return found


def _form_file(form: dict, data: dict) -> str | None:
    file_name = form.get("file")
    if isinstance(file_name, str) and file_name:
        return file_name
    source = data.get("source")
    return source if isinstance(source, str) else None


def _remember_form(history: History, namespace: str, form: dict, data: dict, file_key: str) -> None:
    if _form_file(form, data) != file_key:
        return
    form_id = form.get("id")
    digest = form.get("hash")
    if not isinstance(form_id, str) or not isinstance(digest, str):
        return
    ordinal = form.get("ordinal")
    prior = PriorForm(
        namespace=namespace,
        id=form_id,
        digest=digest,
        file=file_key,
        line=int(form.get("line") or 0),
        ordinal=ordinal if isinstance(ordinal, int) else None,
    )
    history.forms[(namespace, form_id)] = prior
    if isinstance(ordinal, int):
        history.declaration_forms[(namespace, form_id, ordinal)] = prior


def _remember_forms(history: History, data: dict, namespace: str, file_key: str) -> None:
    for form in _forms_of(data):
        _remember_form(history, namespace, form, data, file_key)


def _remember_outcomes(history: History, data: dict, file_key: str) -> None:
    for mutation, status in _outcomes_of(data).items():
        if mutation_file(mutation) == file_key:
            history.outcomes[mutation] = status


def _file_classifier_version(data: dict, file_key: str) -> int | None:
    by_file = data.get("classifier-versions")
    if isinstance(by_file, dict):
        v = by_file.get(file_key)
        if isinstance(v, int):
            return v
        return None
    cv = data.get("classifier-version")
    return cv if isinstance(cv, int) else None


def load_history(root: Path, file_key: str) -> History:
    """Prior form hashes, outcomes, and classifier versions recorded for `file_key`."""

    history = History()
    for path in _edn_files(root):
        data = _read(path)
        if data is None:
            continue
        namespace = data.get("namespace")
        if not isinstance(namespace, str):
            continue
        cv = _file_classifier_version(data, file_key)
        if cv is not None:
            history.classifier_versions[namespace] = cv
        _remember_forms(history, data, namespace, file_key)
        _remember_outcomes(history, data, file_key)
    return history


def _render_form(form: FormResult) -> dict:
    entry = {
        "id": form.id,
        "kind": "defn-" if form.private else "defn",
        "file": form.file,
        "line": form.line,
        "end-line": form.end_line,
        "hash": form.digest,
        "killed": form.killed,
        "survived": form.survived,
        "uncovered": form.uncovered,
        "sites": form.sites,
    }
    if form.ordinal is not None:
        entry["ordinal"] = form.ordinal
    if form.invalid:
        entry["invalid"] = form.invalid
    if form.errors:
        entry["errors"] = form.errors
    if form.incomplete:
        entry["incomplete"] = True
    return entry


def _render_snapshot(
    namespace: str,
    source: str,
    forms: list[dict],
    outcomes: dict[str, str],
    classifier_versions: dict[str, int] | None = None,
) -> str:
    payload: dict = {
        "version": 1,
        "tested-at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source,
        "namespace": namespace,
        "outcomes": {key: keyword(outcomes[key]) for key in sorted(outcomes)},
        "forms": forms,
    }
    if classifier_versions:
        payload["classifier-versions"] = {
            k: classifier_versions[k] for k in sorted(classifier_versions)
        }
        # Only set top-level classifier-version when every file present in the
        # snapshot shares the same version (never stamping retained legacy files).
        files_in_snap = {str(f.get("file")) for f in forms if f.get("file")} | {
            mf for m in outcomes if (mf := mutation_file(m))
        }
        if files_in_snap and files_in_snap.issubset(classifier_versions):
            vals = {classifier_versions[f] for f in files_in_snap}
            if len(vals) == 1:
                payload["classifier-version"] = next(iter(vals))
    if any(form.get("incomplete") for form in forms):
        payload["incomplete"] = True
    text = dumps(payload)
    # Keep the file readable. dumps is one line; break after the top-level pairs
    # is unnecessary because clojure.edn reads either shape.
    return text + "\n"


def _kept_forms(data: dict, file_key: str) -> list[dict]:
    return [form for form in _forms_of(data) if _form_file(form, data) != file_key]


def _kept_outcomes(data: dict, file_key: str) -> dict[str, str]:
    return {
        mutation: status
        for mutation, status in _outcomes_of(data).items()
        if mutation_file(mutation) != file_key
    }


def _group_forms(forms: list[FormResult]) -> dict[str, list[FormResult]]:
    incoming: dict[str, list[FormResult]] = {}
    for form in forms:
        incoming.setdefault(form.namespace, []).append(form)
    return incoming


def _file_outcomes(outcomes: dict[str, str], file_key: str) -> dict[str, str]:
    found = {}
    for mutation, status in outcomes.items():
        if mutation_file(mutation) != file_key:
            continue
        if status not in _RECORDABLE_OUTCOMES:
            continue
        found[mutation] = status
    return found


def _read_snapshots(root: Path) -> list[tuple[Path, dict]]:
    existing = []
    for path in _edn_files(root):
        data = _read(path)
        if data is not None:
            existing.append((path, data))
    return existing


def _mentions_file(data: dict, file_key: str) -> bool:
    for form in _forms_of(data):
        if _form_file(form, data) == file_key:
            return True
    return False


def _kept_classifier_versions(data: dict, file_key: str) -> dict[str, int]:
    kept: dict[str, int] = {}
    by_file = data.get("classifier-versions")
    if isinstance(by_file, dict):
        for f, v in by_file.items():
            if isinstance(f, str) and isinstance(v, int) and f != file_key:
                kept[f] = v
        return kept
    cv = data.get("classifier-version")
    if isinstance(cv, int):
        for form in _kept_forms(data, file_key):
            f = _form_file(form, data)
            if f and f != file_key:
                kept[f] = cv
        for m in _kept_outcomes(data, file_key):
            mf = mutation_file(m)
            if mf and mf != file_key:
                kept[mf] = cv
    return kept


def _keep(retained: dict, namespace: str, data: dict, file_key: str) -> None:
    kept_forms = _kept_forms(data, file_key)
    kept_outcomes = _kept_outcomes(data, file_key)
    kept_versions = _kept_classifier_versions(data, file_key)
    if not kept_forms and not kept_outcomes:
        return
    forms_so_far, outcomes_so_far, versions_so_far = retained.get(namespace, ([], {}, {}))
    forms_so_far.extend(kept_forms)
    outcomes_so_far.update(kept_outcomes)
    versions_so_far.update(kept_versions)
    retained[namespace] = (forms_so_far, outcomes_so_far, versions_so_far)


def _retain(existing, file_key: str):
    retained: dict[str, tuple[list[dict], dict[str, str], dict[str, int]]] = {}
    previous: set[str] = set()
    for _path, data in existing:
        namespace = data.get("namespace")
        if not isinstance(namespace, str):
            continue
        if _mentions_file(data, file_key):
            previous.add(namespace)
        _keep(retained, namespace, data, file_key)
    return retained, previous


def _outcomes_for(outcomes: dict[str, str], namespace: str) -> dict[str, str]:
    found = {}
    for mutation, status in outcomes.items():
        if mutation_namespace(mutation) == namespace:
            found[mutation] = status
    return found


def _form_order(form: dict) -> tuple:
    return (str(form.get("file")), int(form.get("line") or 0), str(form.get("id")))


def _snapshot_source(fresh: list[FormResult], kept_forms: list[dict], file_key: str) -> str:
    if fresh:
        return fresh[0].file
    return str(kept_forms[0].get("file") or file_key)


def _write_namespace(
    root: Path,
    namespace: str,
    kept_forms: list[dict],
    kept_outcomes: dict[str, str],
    fresh: list[FormResult],
    outcomes: dict[str, str],
    file_key: str,
    *,
    kept_versions: dict[str, int] | None = None,
    classifier_version: int | None = None,
) -> Path | None:
    rendered = _dedupe(kept_forms + [_render_form(form) for form in fresh])
    rendered.sort(key=_form_order)
    merged = dict(kept_outcomes)
    if fresh:
        merged.update(_outcomes_for(outcomes, namespace))
    versions = dict(kept_versions or {})
    if fresh and classifier_version is not None:
        versions[file_key] = classifier_version
    path = snapshot_path(root, namespace)
    if not rendered and not merged:
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _render_snapshot(
            namespace,
            _snapshot_source(fresh, kept_forms, file_key),
            rendered,
            merged,
            classifier_versions=versions or None,
        ),
        encoding="utf-8",
    )
    return path


def _stale(path: Path, data: dict, touched: set[str], canonical: set[Path], file_key: str) -> bool:
    if path.resolve() in canonical:
        return False
    namespace = data.get("namespace")
    if isinstance(namespace, str) and namespace in touched:
        return True
    return _form_file_only(data, file_key)


def _drop_stale(existing, touched: set[str], canonical: set[Path], file_key: str) -> None:
    """Remove a per-file snapshot so the viewer cannot prefer it over the namespace file."""

    for path, data in existing:
        if _stale(path, data, touched, canonical, file_key):
            path.unlink(missing_ok=True)


def write_results(
    root: Path,
    file_key: str,
    forms: list[FormResult],
    outcomes: dict[str, str],
    *,
    classifier_version: int | None = None,
) -> list[Path]:
    """Replace this file's forms, preserving other files that share a namespace.

    One snapshot file per namespace. Older per-file snapshots for a namespace
    this write touches are folded in and removed, so uml-viewer sees a single
    map for that class.
    """

    root = root.resolve()
    incoming = _group_forms(forms)
    incoming_outcomes = _file_outcomes(outcomes, file_key)
    existing = _read_snapshots(root)
    retained, previous = _retain(existing, file_key)
    touched = set(incoming) | previous | set(retained)
    written: list[Path] = []
    for namespace in sorted(touched):
        kept_forms, kept_outcomes, kept_versions = retained.get(namespace, ([], {}, {}))
        path = _write_namespace(
            root,
            namespace,
            kept_forms,
            kept_outcomes,
            incoming.get(namespace, []),
            incoming_outcomes,
            file_key,
            kept_versions=kept_versions,
            classifier_version=classifier_version if namespace in incoming else None,
        )
        if path is not None:
            written.append(path)
    canonical = {snapshot_path(root, namespace).resolve() for namespace in touched}
    _drop_stale(existing, touched, canonical, file_key)
    _remove_empty_dirs(root / ".metrics" / "mutate")
    return written


def _form_file_only(data: dict, file_key: str) -> bool:
    forms = _forms_of(data)
    if not forms:
        return data.get("source") == file_key
    files = {_form_file(form, data) for form in forms}
    return files == {file_key}


def _dedupe(forms: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for form in forms:
        key = (form.get("file"), form.get("id"), form.get("line"), form.get("ordinal"), form.get("hash"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(form)
    return unique


def _remove_empty_dirs(directory: Path) -> None:
    if not directory.is_dir():
        return
    for child in sorted(directory.rglob("*"), reverse=True):
        if child.is_dir() and not any(child.iterdir()):
            child.rmdir()
