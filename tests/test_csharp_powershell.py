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
