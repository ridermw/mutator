"""Tests for Tier 1 & Tier 2 mutator upgrades (PRs #14, #16, #22, #23, #39)."""

import subprocess
import sys

from mutator.engine import mutate_file
from mutator.functions import sites_in_file
from mutator.runner import CommandResult, CommandRunner
from mutator.workers import create_workers, delete_tree, new_run_dir


def test_mutator_equivalent_comment_skips_line(tmp_path):
    src = (
        "def clamp(x, lo, hi):\n"
        "    if x < lo:  # mutator:equivalent\n"
        "        return lo\n"
        "    if x > hi:\n"
        "        return hi\n"
        "    return x\n"
    )
    p = tmp_path / "clamp.py"
    p.write_text(src, encoding="utf-8")
    sites = sites_in_file(src, p, tmp_path, "clamp.py")
    assert [s.original for s in sites] == [">"]


def test_moved_function_keeps_each_mutant_outcome(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    pair = "def pair(a, b, c, d):\n    return a > b, c > d\n"
    path.write_text(pair, encoding="utf-8")

    class SecondOnly:
        verbose = False

        def run(self, command, cwd, timeout):
            ns: dict = {}
            exec((cwd / "src" / "demo.py").read_text(encoding="utf-8"), ns)
            ok = ns["pair"](0, 0, 1, 1)[1] is False
            return CommandResult(code=0 if ok else 1, timed_out=False, seconds=0.01, output="")

    def run_once():
        return mutate_file(
            path,
            tmp_path,
            runner=SecondOnly(),
            covered_lines=None,
            ignore_coverage=True,
            mutate_all=False,
            lines=None,
            test_command="fake",
            timeout_factor=10,
            mutation_warning=50,
            baselines={},
        ).forms[0]

    first = run_once()
    assert (first.killed, first.survived) == (1, 1)
    path.write_text("# abcd\n" + pair, encoding="utf-8")
    moved = run_once()
    assert (moved.killed, moved.survived) == (1, 1)


def test_edit_saved_during_run_is_preserved(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    edited = "def place(x):\n    return x > 0\n\n\ndef keep(x):\n    return x\n"

    class EditDuringRun:
        verbose = False

        def run(self, command, cwd, timeout):
            if timeout is not None:
                path.write_text(edited, encoding="utf-8")
            return CommandResult(code=1 if timeout else 0, timed_out=False, seconds=0.01, output="")

    mutate_file(
        path,
        tmp_path,
        runner=EditDuringRun(),
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
        max_workers=1,
    )
    assert path.read_text(encoding="utf-8") == edited


def test_non_utf8_test_output_does_not_abort(tmp_path):
    for code in (0, 1):
        res = CommandRunner().run(f"printf 'caf\\303( ok\\n'; exit {code}", tmp_path, 5)
        assert res.code == code
        assert "ok" in res.output


def test_git_tracked_build_dir_is_linked_into_worker(tmp_path):
    src = tmp_path / "src" / "main" / "Demo.java"
    src.parent.mkdir(parents=True)
    src.write_text("class Demo {}\n", encoding="utf-8")
    gen = tmp_path / "src" / "build" / "java" / "Gen.java"
    gen.parent.mkdir(parents=True)
    gen.write_text("class Gen {}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    untracked_build = tmp_path / "build" / "out.txt"
    untracked_build.parent.mkdir()
    untracked_build.write_text("bin", encoding="utf-8")

    base = new_run_dir(tmp_path)
    try:
        worker = create_workers(base, tmp_path, "src/main/Demo.java", src.read_bytes(), 1)[0]
        assert (worker / "src" / "build" / "java" / "Gen.java").read_text(encoding="utf-8") == "class Gen {}\n"
        assert not (worker / "build").exists()
    finally:
        delete_tree(base)
