"""Tests for C# and PowerShell mutation sites, honest outcome classification, worker isolation, and live Pester execution."""

from pathlib import Path
import shutil

import pytest

from mutator.edn import dumps, keyword, loads
from mutator.engine import _forms, _select, mutate_file
from mutator.functions import is_private, sites_in_file
from mutator.metrics import CLASSIFIER_VERSION, load_history, snapshot_path, write_results
from mutator.model import FormResult, History, PriorForm
from mutator.runner import (
    CommandResult,
    CommandRunner,
    execute_csharp_phased,
    parse_nunit_outcome,
    parse_trx_outcome,
    test_plan as build_test_plan,
)
from mutator.workers import create_workers, delete_tree, new_run_dir


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_csharp_sites_privacy_and_no_null_coalesce_mutation(tmp_path):
    src = """\
namespace Demo;
public class Calc {
    public int Positive(int x, string? s) {
        if (!string.IsNullOrEmpty(s) && x > 0) {
            return (s?.Length ?? 0) + 1;
        }
        return -x;
    }
    private int Secret(int x) => x == 1 ? 0 : 1;
}
"""
    p = _write(tmp_path, "src/Calc.cs", src)
    sites = sites_in_file(src, p, tmp_path, "src/Calc.cs")
    originals = {s.original for s in sites}
    assert {"!", "&&", ">", "?", "+", "1", "-", "==", "0"} <= originals
    assert "??" not in originals
    assert not is_private("csharp", "Positive", src, 3, 8)
    assert is_private("csharp", "Secret", src, 9, 9)


def test_powershell_sites_and_privacy(tmp_path):
    src = """\
function Get-Public {
    param([int]$x)
    if (-not ($x -gt 0 -and $true)) { return 0 }
    return $x + 1
}
function Get-Internal {
    param([string]$s)
    if ($s -ceq 'ok') { return $true }
    return $false
}
Export-ModuleMember -Function Get-Public
"""
    p = _write(tmp_path, "src/Mod.psm1", src)
    sites = sites_in_file(src, p, tmp_path, "src/Mod.psm1")
    pairs = {(s.original, s.mutant) for s in sites}
    assert ("-not", "") in pairs
    assert ("-gt", "-ge") in pairs
    assert ("-and", "-or") in pairs
    assert ("$true", "$false") in pairs
    assert ("-ceq", "-cne") in pairs
    assert ("+", "-") in pairs
    assert not is_private("powershell", "Get-Public", src, 1, 5)
    assert is_private("powershell", "Get-Internal", src, 6, 10)


def test_honest_outcome_classification_invalid_error_and_versioned_cache(tmp_path):
    p = _write(tmp_path, "src/Calc.cs", "namespace Demo;\npublic class Calc { public int F(int x) { return x > 0 ? x + 1 : 0; } }\n")
    src = p.read_text(encoding="utf-8")
    sites = sites_in_file(src, p, tmp_path, "src/Calc.cs")
    covered = {s.mutation_id: True for s in sites}

    # Invalid-only form has no score (None), NOT 100%; unknown status becomes error + incomplete
    outcomes = {sites[0].mutation_id: "invalid"}
    for s in sites[1:]:
        outcomes[s.mutation_id] = "invalid"
    forms = _forms(src, p, tmp_path, "src/Calc.cs", sites, covered, outcomes, None)
    assert len(forms) == 1
    assert forms[0].invalid == len(sites)
    assert forms[0].score is None
    assert not forms[0].incomplete

    # Unknown status becomes error + incomplete (never falls back to survived)
    bad_outcomes = {sites[0].mutation_id: "mystery_state"}
    bad_forms = _forms(src, p, tmp_path, "src/Calc.cs", sites[:1], {sites[0].mutation_id: True}, bad_outcomes, None)
    assert bad_forms[0].survived == 0
    assert bad_forms[0].errors == 1
    assert bad_forms[0].incomplete is True

    # Legacy C# cached kill without classifier-version 2 must be re-selected
    legacy = History(
        forms={("Demo.Calc", "defn/F"): PriorForm("Demo.Calc", "defn/F", "d", "src/Calc.cs")},
        outcomes={sites[0].mutation_id: "killed"},
        classifier_versions={},
    )
    selected = _select(sites[:1], {sites[0].mutation_id: True}, legacy, {("Demo.Calc", "defn/F"): "d"}, False, None, language="csharp")
    assert len(selected) == 1

    # Versioned C# cached kill with CLASSIFIER_VERSION IS skipped
    versioned = History(
        forms=legacy.forms,
        outcomes=legacy.outcomes,
        classifier_versions={"Demo.Calc": CLASSIFIER_VERSION},
    )
    selected_v = _select(sites[:1], {sites[0].mutation_id: True}, versioned, {("Demo.Calc", "defn/F"): "d"}, False, None, language="csharp")
    assert selected_v == []

    # EDN round-trip persists invalid/errors/incomplete/classifier-version
    written = write_results(tmp_path, "src/Calc.cs", bad_forms, {sites[0].mutation_id: "error"}, classifier_version=CLASSIFIER_VERSION)
    raw = loads(written[0].read_text(encoding="utf-8"))
    assert raw["classifier-version"] == CLASSIFIER_VERSION
    assert raw["incomplete"] is True
    assert raw["forms"][0]["errors"] == 1


def test_csharp_phased_execution_and_trx_classification(tmp_path):
    trx = tmp_path / "results.trx"
    trx.write_text('<TestRun><ResultSummary><Counters total="2" executed="2" passed="1" failed="1" error="0" timeout="0" /></ResultSummary></TestRun>')
    assert parse_trx_outcome(trx) == "killed"
    trx.write_text('<TestRun><ResultSummary><Counters total="2" executed="2" passed="2" failed="0" error="0" timeout="0" /></ResultSummary></TestRun>')
    assert parse_trx_outcome(trx) == "survived"
    trx.write_text('<TestRun><ResultSummary><Counters total="0" executed="0" passed="0" failed="0" error="0" timeout="0" /></ResultSummary></TestRun>')
    assert parse_trx_outcome(trx) == "error"

    class CompilerReject:
        verbose = False

        def run(self, command, cwd, timeout):
            return CommandResult(code=1, timed_out=False, seconds=0.1, output="Calc.cs(4,12): error CS0019: Operator '-' cannot be applied")

    assert execute_csharp_phased(CompilerReject(), tmp_path, "App.sln", 10.0, 10.0) == "invalid"

    class BuildTimeout:
        verbose = False

        def run(self, command, cwd, timeout):
            return CommandResult(code=124, timed_out=True, seconds=10.0, output="")

    assert execute_csharp_phased(BuildTimeout(), tmp_path, "App.sln", 10.0, 10.0) == "error"


def test_csharp_worker_containment_rejects_fsproj_symlinks_and_escaping_paths(tmp_path):
    proj = tmp_path / "repo"
    src = _write(proj, "src/Calc.cs", "class Calc { int F(int x) => x + 1; }\n")
    _write(proj, "App.sln", 'Project("{F2A71F9B-5D33-465A-A702-920D77279786}") = "Lib", "Lib/Lib.fsproj", "{1}"\n')
    base = new_run_dir(proj)
    try:
        with pytest.raises(ValueError, match="Unsupported .NET project reference"):
            create_workers(base, proj, "src/Calc.cs", src.read_bytes(), 1)
    finally:
        delete_tree(base)

    # Reject --no-build and absolute project paths in custom override
    with pytest.raises(ValueError, match="--no-build"):
        build_test_plan(proj, src, "csharp", "dotnet test --no-build")
    with pytest.raises(ValueError, match="relative .NET project paths"):
        build_test_plan(proj, src, "csharp", f"dotnet test {proj}/App.sln")

    # Reject absolute ProjectReference and absolute OutputPath even when inside --root
    proj2 = tmp_path / "absrepo"
    src2 = _write(proj2, "src/Calc.cs", "class Calc { int F(int x) => x + 1; }\n")
    _write(
        proj2,
        "App.csproj",
        f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputPath>{proj2}/out</OutputPath></PropertyGroup></Project>\n',
    )
    base2 = new_run_dir(proj2)
    try:
        with pytest.raises(ValueError, match="Absolute C# output path"):
            create_workers(base2, proj2, "src/Calc.cs", src2.read_bytes(), 1)
    finally:
        delete_tree(base2)

    proj3 = tmp_path / "absref"
    src3 = _write(proj3, "src/Calc.cs", "class Calc { int F(int x) => x + 1; }\n")
    _write(
        proj3,
        "App.csproj",
        f'<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><ProjectReference Include="{proj3}/Lib.csproj" /></ItemGroup></Project>\n',
    )
    base3 = new_run_dir(proj3)
    try:
        with pytest.raises(ValueError, match="Absolute project reference"):
            create_workers(base3, proj3, "src/Calc.cs", src3.read_bytes(), 1)
    finally:
        delete_tree(base3)


def test_same_line_csharp_overloads_privacy_body_strings_and_per_file_classifier_versions(tmp_path):
    # Two overloads on the same line keep distinct ordinals and sites without double counting
    src = 'namespace Demo;\npublic class C { public string M(int a) => a > 0 ? "private" : "ok"; public int M(long b) => b < 1 ? 1 : 0; }\n'
    p = _write(tmp_path, "src/C.cs", src)
    sites = sites_in_file(src, p, tmp_path, "src/C.cs")
    assert {s.form_ordinal for s in sites} == {0, 1}
    assert not is_private("csharp", "M", src, 2, 2)
    ps_src = "function Get-Msg { return 'hidden' }\n"
    assert not is_private("powershell", "Get-Msg", ps_src, 1, 1)

    covered = {s.mutation_id: True for s in sites}
    outcomes = {s.mutation_id: "killed" for s in sites}
    forms = _forms(src, p, tmp_path, "src/C.cs", sites, covered, outcomes, None)
    assert len(forms) == 2
    assert sum(f.killed for f in forms) == len(sites)

    # Differential cache hits on unchanged declaration without rerunning
    write_results(tmp_path, "src/C.cs", forms, outcomes, classifier_version=CLASSIFIER_VERSION)
    hist = load_history(tmp_path, "src/C.cs")
    from mutator.functions import form_declarations

    digests = {(ns, fid, ord_): dg for ns, fid, _s, _e, _nm, _pr, dg, ord_ in form_declarations(src, p, tmp_path)}
    assert _select(sites, covered, hist, digests, False, None, language="csharp") == []

    # Writing an untouched namespace preserves its classifier-version, and legacy partial file stays unversioned
    other_path = snapshot_path(tmp_path, "Demo.Other")
    other_path.parent.mkdir(parents=True, exist_ok=True)
    other_path.write_text(
        dumps({"version": 1, "source": "src/Other.cs", "namespace": "Demo.Other", "classifier-version": 2, "outcomes": {}, "forms": [{"id": "defn/X", "file": "src/Other.cs", "line": 1, "hash": "h"}]}) + "\n",
        encoding="utf-8",
    )
    # Add a legacy (unversioned) partial file to Demo.C before rewriting src/C.cs
    legacy_c = loads(snapshot_path(tmp_path, "Demo.C").read_text(encoding="utf-8"))
    legacy_c["forms"].append({"id": "defn/Old", "file": "src/C.Part.cs", "line": 1, "hash": "h0", "killed": 1, "survived": 0, "uncovered": 0, "sites": 1})
    legacy_c.pop("classifier-version", None)
    legacy_c["classifier-versions"] = {"src/C.cs": 2}
    snapshot_path(tmp_path, "Demo.C").write_text(dumps(legacy_c) + "\n", encoding="utf-8")

    write_results(tmp_path, "src/C.cs", forms, outcomes, classifier_version=CLASSIFIER_VERSION)
    assert load_history(tmp_path, "src/Other.cs").classifier_versions.get("Demo.Other") == 2
    assert load_history(tmp_path, "src/C.Part.cs").classifier_versions.get("Demo.C") is None
    assert load_history(tmp_path, "src/C.cs").classifier_versions.get("Demo.C") == 2


def test_load_bundle_compat_with_legacy_crapper_signature(tmp_path):
    from types import SimpleNamespace
    from mutator.coverage import _load_bundle

    called = []
    legacy_crapper = SimpleNamespace(coverage=SimpleNamespace(load_bundle=lambda root: called.append(root) or "ok"))
    assert _load_bundle(legacy_crapper, tmp_path, historical=True) == "ok"
    assert called == [tmp_path]


def test_mutator_round2_fixes_dynamic_output_lines_incomplete_same_line_privacy_multi_trx_and_scan(tmp_path):
    from mutator.engine import scan_file
    from mutator.runner import parse_trx_directory

    # 1. Dynamic MSBuild OutputPath is rejected
    p_dyn = tmp_path / "dynout"
    s_dyn = _write(p_dyn, "src/C.cs", "class C { int F(int x) => x + 1; }\n")
    _write(
        p_dyn,
        "App.csproj",
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputPath>$(RepoRoot)out/</OutputPath></PropertyGroup></Project>\n',
    )
    b = new_run_dir(p_dyn)
    try:
        with pytest.raises(ValueError, match="Dynamic MSBuild output path"):
            create_workers(b, p_dyn, "src/C.cs", s_dyn.read_bytes(), 1)
    finally:
        delete_tree(b)

    # 2. Same-line class + private method uses byte-span signature, not class header
    same_line = "namespace Demo;\npublic class C { private int Secret(int x) => x + 1; public int Open(int x) => x + 2; }\n"
    p_sl = _write(tmp_path, "sl/C.cs", same_line)
    sl_sites = sites_in_file(same_line, p_sl, tmp_path / "sl", "C.cs")
    by_name = {s.name: s.form_id for s in sl_sites}
    assert by_name["Secret"] == "defn-/Secret"
    assert by_name["Open"] == "defn/Open"

    # 3. --lines partial run marks form incomplete without incrementing errors for unselected sites
    two_lines = "def f(x):\n    a = x > 0\n    return x + 1\n"
    p_ln = _write(tmp_path, "ln/demo.py", two_lines)
    ln_sites = sites_in_file(two_lines, p_ln, tmp_path / "ln", "demo.py")
    covered = {s.mutation_id: True for s in ln_sites}
    line2 = [s for s in ln_sites if s.line == 2]
    outcomes = {s.mutation_id: "killed" for s in line2}
    forms = _forms(two_lines, p_ln, tmp_path / "ln", "demo.py", ln_sites, covered, outcomes, {2})
    assert forms[0].incomplete is True
    assert forms[0].errors == 0

    # 4. Multi-TRX aggregation catches kill in first report even if second report passed
    trx_dir = tmp_path / "trxs"
    trx_dir.mkdir()
    (trx_dir / "a.trx").write_text('<TestRun><ResultSummary><Counters executed="1" failed="1" error="0" timeout="0"/></ResultSummary></TestRun>')
    (trx_dir / "b.trx").write_text('<TestRun><ResultSummary><Counters executed="1" failed="0" error="0" timeout="0"/></ResultSummary></TestRun>')
    assert parse_trx_directory(trx_dir) == "killed"

    # 5. scan_file does not mark unchanged C# function as changed after versioned run
    cs_root = tmp_path / "csscan"
    cs_src = "namespace Demo;\npublic class C { public int F(int x) => x + 1; }\n"
    cs_path = _write(cs_root, "src/C.cs", cs_src)
    cs_sites = sites_in_file(cs_src, cs_path, cs_root, "src/C.cs")
    cs_forms = _forms(cs_src, cs_path, cs_root, "src/C.cs", cs_sites, {s.mutation_id: True for s in cs_sites}, {s.mutation_id: "killed" for s in cs_sites}, None)
    write_results(cs_root, "src/C.cs", cs_forms, {s.mutation_id: "killed" for s in cs_sites}, classifier_version=CLASSIFIER_VERSION)
    scan_out = scan_file(cs_path, cs_root, covered_lines=None, ignore_coverage=True, lines=None)
    assert "* marks a function" not in scan_out

    # 6. Custom --test-command override on C# must NOT stamp classifier-version 2
    class FakePass:
        verbose = False

        def run(self, command, cwd, timeout):
            return CommandResult(code=1 if timeout else 0, timed_out=False, seconds=0.01, output="")

    cs_ov = tmp_path / "csov"
    ov_path = _write(cs_ov, "src/C.cs", cs_src)
    mutate_file(
        ov_path, cs_ov, runner=FakePass(), covered_lines=None, ignore_coverage=True,
        mutate_all=True, lines=None, test_command="fake-override", timeout_factor=10,
        mutation_warning=50, baselines={}, max_workers=1,
    )
    assert load_history(cs_ov, "src/C.cs").classifier_versions == {}


def test_mutator_round3_fixes_importer_tracked_output_compile_import_custom_cmd_and_ps_export(tmp_path):
    import subprocess
    from mutator.functions import is_private

    # 1. Importer expansion must copy tracked output dir, never symlink mutable output dir
    js_root = tmp_path / "jsimp"
    _write(js_root, "src/mod.mjs", "export function add(a, b) { return a + b; }\n")
    _write(js_root, "tests/use.mjs", "import { add } from '../src/mod.mjs';\nexport const x = add(1, 2);\n")
    _write(js_root, "tests/build/gen.txt", "tracked\n")
    _write(js_root, "tests/build/untracked.out", "original\n")
    subprocess.run(["git", "init", "-q"], cwd=js_root, check=True)
    subprocess.run(["git", "add", "src/mod.mjs", "tests/use.mjs", "tests/build/gen.txt"], cwd=js_root, check=True)
    b_js = new_run_dir(js_root)
    try:
        workers = create_workers(b_js, js_root, "src/mod.mjs", (js_root / "src/mod.mjs").read_bytes(), 1)
        w_build = workers[0] / "tests/build"
        assert w_build.is_dir() and not w_build.is_symlink()
        assert (w_build / "gen.txt").read_text() == "tracked\n"
        assert not (w_build / "untracked.out").exists()
    finally:
        delete_tree(b_js)

    # 2. Explicit absolute <Compile Include> is rejected
    p_comp = tmp_path / "abscomp"
    s_comp = _write(p_comp, "src/C.cs", "class C { int F(int x) => x + 1; }\n")
    _write(
        p_comp,
        "App.csproj",
        f'<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><Compile Include="{s_comp.resolve()}" /></ItemGroup></Project>\n',
    )
    b1 = new_run_dir(p_comp)
    try:
        with pytest.raises(ValueError, match="Absolute MSBuild Compile Include"):
            create_workers(b1, p_comp, "src/C.cs", s_comp.read_bytes(), 1)
    finally:
        delete_tree(b1)

    # 3. Escaping <Import Project> and imported file with escaping <OutputPath> are both rejected
    p_imp = tmp_path / "escimp"
    s_imp = _write(p_imp, "src/C.cs", "class C { int F(int x) => x + 1; }\n")
    _write(p_imp, "shared/bad.xml", "<Project><PropertyGroup><OutputPath>/tmp/shared_out</OutputPath></PropertyGroup></Project>\n")
    _write(p_imp, "App.csproj", '<Project Sdk="Microsoft.NET.Sdk"><Import Project="shared/bad.xml" /></Project>\n')
    b2 = new_run_dir(p_imp)
    try:
        with pytest.raises(ValueError, match="Absolute C# output path"):
            create_workers(b2, p_imp, "src/C.cs", s_imp.read_bytes(), 1)
    finally:
        delete_tree(b2)

    # 4. Custom --test-command on C# discards prior phased-runner kills instead of skipping execution
    cs_cached = tmp_path / "cscached"
    cs_src = "namespace Demo;\npublic class C { public int F(int x) => x + 1; }\n"
    cs_p = _write(cs_cached, "src/C.cs", cs_src)
    cs_sites = sites_in_file(cs_src, cs_p, cs_cached, "src/C.cs")
    cs_forms = _forms(
        cs_src, cs_p, cs_cached, "src/C.cs", cs_sites,
        {s.mutation_id: True for s in cs_sites},
        {s.mutation_id: "killed" for s in cs_sites},
        None,
    )
    write_results(
        cs_cached, "src/C.cs", cs_forms, {s.mutation_id: "killed" for s in cs_sites},
        classifier_version=CLASSIFIER_VERSION,
    )
    ran = []

    class RecordingRunner:
        verbose = False

        def run(self, command, cwd, timeout):
            ran.append(command)
            return CommandResult(code=0, timed_out=False, seconds=0.01, output="")

    mutate_file(
        cs_p, cs_cached, runner=RecordingRunner(), covered_lines=None, ignore_coverage=True,
        mutate_all=False, lines=None, test_command="custom-cmd", timeout_factor=10,
        mutation_warning=50, baselines={}, max_workers=1,
    )
    assert len(ran) > 0

    # 5. Export-ModuleMember -Function stops at next parameter (-Alias)
    ps_src = "function Get-Public { return 1 }\nExport-ModuleMember -Function Get-Public -Alias gp\n"
    assert not is_private("powershell", "Get-Public", ps_src, 1, 1)


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
def test_powershell_live_mutation_isolated_worker(tmp_path):
    src_text = """\
function Add-Positive {
    param([int]$a, [int]$b)
    if ($a -gt 0) {
        return $a + $b
    }
    return 0
}
"""
    src = _write(tmp_path, "src/Calc.ps1", src_text)
    _write(
        tmp_path,
        "tests/Calc.Tests.ps1",
        """\
BeforeAll {
    . "$PSScriptRoot/../src/Calc.ps1"
}
Describe 'Add-Positive' {
    It 'adds when positive' {
        Add-Positive -a 2 -b 3 | Should -Be 5
    }
    It 'returns 0 at zero' {
        Add-Positive -a 0 -b 3 | Should -Be 0
    }
}
""",
    )
    original_bytes = src.read_bytes()
    result = mutate_file(
        src,
        tmp_path,
        runner=CommandRunner(),
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command=None,
        timeout_factor=10.0,
        mutation_warning=50,
        baselines={},
        max_workers=1,
    )
    assert not result.baseline_failed, result.baseline_message
    assert not result.incomplete
    assert src.read_bytes() == original_bytes
    assert len(result.forms) == 1
    assert result.forms[0].killed >= 2
    assert result.forms[0].errors == 0
