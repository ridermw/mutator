---
name: mutator
description: Use when running mutation tests, checking whether tests actually pin behavior, when mutator prints SURVIVED or UNCOVERED or exits 2 or 3, when considering --mutate-all, or when .metrics/mutate must be refreshed for uml-viewer, in Clojure, Java, Go, TypeScript, Rust, or Python projects.
---

# mutator

## Overview

mutator changes one operator or constant at a time (`<` to `<=`, `and` to
`or`, `True` to `False`, `0` to `1`) and runs the tests. **Killed:** a test
failed. **Survived:** the tests still pass, so nothing pins that behavior.
**Uncovered:** no test reaches the line, so it is not run. The score is
killed / (killed + survived).

## Run

Run from the examined project's root. Use `mutator` if it is on `PATH`;
otherwise run `<base>/../../../mutator`, where `<base>` is this skill's base
directory. It needs crapper installed or checked out beside it (`../crapper`).

| Command | Use |
|---|---|
| `mutator src/pkg/pricing.py` | one file or directory. **Start with one file:** the first run executes every covered site |
| `mutator --scan src/pkg` | list sites without running tests |
| `mutator --changed` | git additions and edits |
| `mutator --test-command "<cmd>"` | faster or custom test command |
| `mutator --max-workers 2 FILE` | cap parallel workers |

Exit 0: every mutant was killed. **Exit 3: a mutant survived. That is a
result, not an error;** read the SURVIVED lines. Exit 2: the baseline tests
fail and nothing was written; fix the tests first.

## Differential is the default

Once `.metrics/mutate/` has a snapshot, a run reruns survivors and every site
in a function whose text changed. Killed mutants in unchanged functions stay
killed, which is correct because their tests still exist.

Use `--mutate-all` only when a test or assertion was deleted or weakened
since the last run, or the user asked for it.

| Rationalization | Reality |
|---|---|
| "So nothing carries over" or "a clean slate" | Carried-over kills are still valid. Run the file. |
| "It exited 3, so rerun with `--mutate-all`" | Exit 3 is the answer. Kill the survivors. |
| "Some sites are UNCOVERED" | `--mutate-all` skips them too. Add tests. |

## Killing survivors

- Write the test that tells the mutant apart: the exact boundary for `<` and
  `<=`, exact values for arithmetic, each side of `and` and `or`.
- A mutant no input can tell apart (equivalent), or one in unreachable code:
  report it with file and line. Remove dead code only while tests stay green.
- **Python multi-line conditions:** coverage.py records only a statement's
  first line, so a site on a continuation line shows UNCOVERED even when the
  test runs it.

  ```python
  if (order.total >= 500
          and order.customer.is_member):   # 'and' shows UNCOVERED
  ```

  Fix the code, not the tests: join the condition onto one line or extract a
  named predicate (`if qualifies(order):`). Then rerun the file.

## `.metrics/mutate/` is the next run's input

Never delete it; that discards every kill and forces a full rerun. Each run
also rewrites `:tested-at` in every snapshot, which is expected churn.

- `git ls-files .metrics` lists files: commit the refreshed snapshots with
  the change.
- It lists nothing: leave `.metrics/` untracked and tell the user it is
  there (uml-viewer recommends committing it).

## Red flags

- Typing `--mutate-all` without a deleted or weakened test.
- Deleting `.metrics/` "so the tree is tidy".
- Adding tests for an UNCOVERED site on a continuation line.

## More

- [mutator README](../../../README.md)
- [clj-mutate](https://github.com/unclebob/clj-mutate), the Clojure original
- [Mutation Testing](https://blog.cleancoder.com/uncle-bob/2016/06/10/MutationTesting.html) by Robert C. Martin
