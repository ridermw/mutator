"""Run a project's tests, and decide the command for each language."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

Command = str | list[str]


@dataclass(frozen=True)
class CommandResult:
    code: int
    timed_out: bool
    seconds: float
    output: str


def _worker_home(cwd: Path) -> Path | None:
    """The overlay root when cwd is inside target/mutation-workers."""

    for candidate in [cwd, *cwd.parents]:
        parent = candidate.parent
        if not candidate.name.startswith("worker-"):
            continue
        if not parent.name.startswith("run-"):
            continue
        if parent.parent.name == "mutation-workers":
            return candidate
    return None


def _source_entries(cwd: Path, worker: Path) -> list[str]:
    found = []
    for directory in (cwd / "src", cwd, worker / "src", worker):
        text = str(directory)
        if text in found:
            continue
        if directory.is_dir():
            found.append(text)
    return found


def _prefer_worker_sources(cwd: Path, environment: dict) -> None:
    """An editable install would otherwise import the unmutated tree."""

    worker = _worker_home(cwd)
    if worker is None:
        return
    entries = _source_entries(cwd, worker)
    current = environment.get("PYTHONPATH")
    if current:
        entries.append(current)
    if not entries:
        return
    environment["PYTHONPATH"] = os.pathsep.join(entries)


def display_command(command: Command) -> str:
    """A shell-looking rendering. A list is quoted so it can be pasted."""

    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


class CommandRunner:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def run(self, command: Command, cwd: Path, timeout: float | None) -> CommandResult:
        """Run `command` in `cwd`.

        A list is an argument vector and does not go through a shell, so a
        path with spaces or metacharacters stays one argument. A string is a
        command the user typed in `--test-command` or `--coverage-command`.
        """

        if self.verbose:
            print(f"+ ({cwd}) {display_command(command)}", file=sys.stderr)
        started = time.monotonic()
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        _prefer_worker_sources(cwd, environment)
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                shell=isinstance(command, str),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
        except OSError as exc:
            seconds = time.monotonic() - started
            return CommandResult(code=127, timed_out=False, seconds=seconds, output=str(exc))
        try:
            output, _err = process.communicate(timeout=timeout)
            code = process.returncode if process.returncode is not None else 1
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _err = process.communicate()
            code = 124
            timed_out = True
        seconds = time.monotonic() - started
        return CommandResult(code=code, timed_out=timed_out, seconds=seconds, output=output or "")


def nearest(start: Path, marker: str, stop: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    stop = stop.resolve()
    current = current.resolve()
    while True:
        if (current / marker).is_file():
            return current
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _clojure_command(directory: Path) -> list[str]:
    deps = directory / "deps.edn"
    bb = directory / "bb.edn"
    if bb.is_file() and not deps.is_file():
        text = _read(bb)
        if "spec" in text:
            return ["bb", "spec", "--tag", "~no-mutate"]
        return ["bb", "test"]
    text = _read(deps)
    if ":spec" in text and "speclj" in text:
        return ["clj", "-M:spec", "--tag", "~no-mutate"]
    if ":spec" in text:
        return ["clj", "-M:spec"]
    return ["clj", "-M:test"]


def _project_interpreter(directory: Path) -> str:
    """The project's virtualenv, or this process when the project has none.

    The path stays absolute and the symlink is left in place. A worker does
    not link ``.venv``, and resolving ``bin/python`` would leave the virtualenv.
    """

    for name in (".venv", "venv"):
        candidate = (directory / name / "bin" / "python").absolute()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return sys.executable


def _python_command(directory: Path) -> list[str]:
    interpreter = _project_interpreter(directory)
    pyproject = _read(directory / "pyproject.toml")
    if (
        (directory / "pytest.ini").is_file()
        or (directory / "conftest.py").is_file()
        or "pytest" in pyproject
        or (directory / "setup.cfg").is_file() and "pytest" in _read(directory / "setup.cfg")
    ):
        return [interpreter, "-m", "pytest"]
    return [interpreter, "-m", "unittest", "discover"]


def _nearest_marker(source: Path, root: Path, markers: tuple[str, ...]) -> Path:
    for marker in markers:
        found = nearest(source, marker, root)
        if found is not None:
            return found
    return root


def _go_package(directory: Path, source: Path) -> str:
    relative = source.resolve().parent.relative_to(directory.resolve())
    if relative == Path("."):
        return "."
    return "./" + relative.as_posix()


def _clojure_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("deps.edn", "bb.edn"))
    return _clojure_command(directory), directory


def _java_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("pom.xml",))
    return ["mvn", "-q", "test", "-DexcludeTags=no-mutate"], directory


def _go_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("go.mod",))
    return ["go", "test", "-count=1", _go_package(directory, source)], directory


def _typescript_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("package.json",))
    return ["npm", "test"], directory


def _rust_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("Cargo.toml",))
    return ["cargo", "test"], directory


def _python_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("pyproject.toml", "pytest.ini", "setup.cfg"))
    return _python_command(directory), directory


def _csharp_plan(root: Path, source: Path) -> tuple[Command, Path]:
    from mutator.crapper_link import ensure_crapper

    target = ensure_crapper().runners.dotnet_target(root, [source])
    rel = target.resolve().relative_to(root.resolve()).as_posix()
    return ["dotnet", "test", rel, "--nologo", "--filter", "Category!=no-mutate"], root


def _powershell_test_command(worker_root: Path, result_xml: Path) -> list[str]:
    root_q = str(worker_root.resolve()).replace("'", "''")
    xml_q = str(result_xml.resolve()).replace("'", "''")
    src_dir = worker_root / "src"
    module_roots = [str(worker_root.resolve())]
    if src_dir.is_dir():
        module_roots.insert(0, str(src_dir.resolve()))
    ps_prefix = os.pathsep.join(module_roots).replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$env:PSModulePath = '{ps_prefix}' + [IO.Path]::PathSeparator + $env:PSModulePath; "
        "Import-Module Pester -MinimumVersion 5.7.0 -Force; "
        "$cfg = New-PesterConfiguration; "
        f"$cfg.Run.Path = '{root_q}'; "
        "$cfg.Run.PassThru = $true; "
        "$cfg.Filter.ExcludeTag = @('no-mutate'); "
        "$cfg.CodeCoverage.Enabled = $false; "
        "$cfg.TestResult.Enabled = $true; "
        "$cfg.TestResult.OutputFormat = 'NUnitXml'; "
        f"$cfg.TestResult.OutputPath = '{xml_q}'; "
        "$res = Invoke-Pester -Configuration $cfg; "
        "if ($res.TotalCount -eq 0) { exit 2 }; "
        "if ($res.FailedCount -gt 0) { exit 1 }; exit 0"
    )
    return ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script]


def _powershell_plan(root: Path, source: Path) -> tuple[Command, Path]:
    del source
    return _powershell_test_command(root, root / "target" / "mutator-pester-results.xml"), root


_PLANS = {
    "clojure": _clojure_plan,
    "java": _java_plan,
    "csharp": _csharp_plan,
    "go": _go_plan,
    "typescript": _typescript_plan,
    "rust": _rust_plan,
    "python": _python_plan,
    "powershell": _powershell_plan,
}


def test_plan(root: Path, source: Path, language: str, override: str | None) -> tuple[Command, Path]:
    """The test command and the directory it runs in.

    A user override stays a shell string. Every generated command is an
    argument vector, so a package path is one argument.
    """

    root = root.resolve()
    if override:
        if language == "csharp":
            parts = shlex.split(override)
            if any(part.split("=", 1)[0] == "--no-build" for part in parts):
                raise ValueError(
                    "C# mutations require recompilation; --no-build would test the original assembly"
                )
            if any(
                Path(part).is_absolute() and Path(part).suffix.lower() in {".csproj", ".sln", ".slnx"}
                for part in parts
            ):
                raise ValueError(
                    "Use relative .NET project paths so tests build the worker, not the original tree"
                )
        return override, root
    plan = _PLANS.get(language)
    if plan is None:
        return ["false"], root
    return plan(root, source)


def parse_trx_outcome(trx_path: Path) -> str:
    """Classify a VSTest TRX file into 'killed', 'survived', or 'error'."""

    import xml.etree.ElementTree as ET

    if not trx_path.is_file():
        return "error"
    try:
        root = ET.parse(trx_path).getroot()
    except (OSError, ET.ParseError):
        return "error"
    counters = None
    for elem in root.iter():
        if elem.tag.rpartition("}")[2] == "Counters":
            counters = elem
            break
    if counters is None:
        return "error"
    try:
        executed = int(counters.get("executed", "0"))
        failed = int(counters.get("failed", "0"))
        errors = int(counters.get("error", "0"))
        timeout_count = int(counters.get("timeout", "0"))
    except ValueError:
        return "error"
    if executed <= 0:
        return "error"
    if failed > 0 or timeout_count > 0:
        return "killed"
    if errors > 0:
        return "error"
    return "survived"


def parse_nunit_outcome(xml_path: Path) -> str:
    """Classify a Pester NUnitXml result file into 'killed', 'survived', or 'error'."""

    import xml.etree.ElementTree as ET

    if not xml_path.is_file():
        return "error"
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return "error"
    if root.tag != "test-results":
        return "error"
    try:
        total = int(root.get("total", "0"))
        failures = int(root.get("failures", "0"))
        errors = int(root.get("errors", "0"))
    except ValueError:
        return "error"
    if total <= 0:
        return "error"
    if failures > 0:
        return "killed"
    if errors > 0:
        return "error"
    return "survived"


def execute_csharp_phased(
    runner: CommandRunner,
    worker: Path,
    target_rel: str,
    build_timeout: float,
    test_timeout: float,
) -> str:
    """Phased C# mutant execution: build first (compiler errors -> invalid),
    then test with TRX structured evidence (failed tests -> killed, passing -> survived)."""

    import re

    build = runner.run(
        ["dotnet", "build", target_rel, "-c", "Debug", "--nologo", "--no-restore"],
        worker,
        build_timeout,
    )
    if build.timed_out:
        return "error"
    if build.code != 0:
        if re.search(r"\berror\s+CS\d{4}\b", build.output):
            return "invalid"
        return "error"

    trx_dir = worker / ".mutator-trx"
    if trx_dir.exists():
        import shutil
        shutil.rmtree(trx_dir, ignore_errors=True)
    trx_dir.mkdir(parents=True, exist_ok=True)
    trx_file = trx_dir / "results.trx"

    test_res = runner.run(
        [
            "dotnet",
            "test",
            target_rel,
            "-c",
            "Debug",
            "--no-build",
            "--nologo",
            "--filter",
            "Category!=no-mutate",
            "--logger",
            f"trx;LogFileName={trx_file.name}",
            "--results-directory",
            str(trx_dir),
        ],
        worker,
        test_timeout,
    )
    if test_res.timed_out:
        return "killed"
    outcome = parse_trx_outcome(trx_file)
    if outcome == "survived" and test_res.code != 0:
        return "error"
    return outcome


def execute_powershell_phased(
    runner: CommandRunner,
    worker: Path,
    mutated_file: Path,
    test_timeout: float,
) -> str:
    """Phased PowerShell mutant execution: syntax check -> Pester NUnitXml."""

    file_q = str(mutated_file.resolve()).replace("'", "''")
    parse_check = runner.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$tok=$null; $err=$null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{file_q}', [ref]$tok, [ref]$err); "
                "if ($err -and $err.Count -gt 0) { exit 3 }"
            ),
        ],
        worker,
        15.0,
    )
    if parse_check.timed_out:
        return "error"
    if parse_check.code == 3:
        return "invalid"
    if parse_check.code != 0:
        return "error"

    xml_path = worker / ".mutator-pester-results.xml"
    xml_path.unlink(missing_ok=True)
    cmd = _powershell_test_command(worker, xml_path)
    test_res = runner.run(cmd, worker, test_timeout)
    if test_res.timed_out:
        return "killed"
    outcome = parse_nunit_outcome(xml_path)
    if outcome == "survived" and test_res.code != 0:
        return "error"
    return outcome
