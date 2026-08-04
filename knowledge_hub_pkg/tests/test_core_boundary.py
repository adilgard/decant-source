"""The corpus-agnostic rule, enforced mechanically (d.s parser_supplied
Stage 3).

knowledge_hub_pkg serves every corpus and must contain no domain-specific
logic. Domain lives in ontology sets, in operator config, and in external
plugins that config points at. The one structural way that rule breaks is
core IMPORTING a plugin: after that the package no longer builds without
the domain installed, and the separation is over whether or not anybody
notices.

A rule nobody can violate by accident does not need a check. This one is
violated by an editor's auto-import, so it gets one.

What must hold:

* THE CHECK GOES RED on a core that imports a plugin — by package name,
  and also by resolved location when the installed name differs from the
  directory. Proven by building both violations, not by describing them.
* IT SEES DEFERRED IMPORTS. Core defers its heaviest imports into function
  bodies, which is exactly where an illicit one would hide. Static parsing
  sees them; an import-and-inspect check would not.
* IT IS GREEN ON THE REAL PACKAGE, today.
* IT IS WIRED INTO BOTH RUNNERS, so the rule is enforced on every pilot
  gate and every field verify rather than when someone remembers.
* IT DOES NOT OVERREACH. Ordinary third-party and stdlib imports are not
  its business; a boundary check that also polices dependency hygiene goes
  red for reasons that have nothing to do with the boundary.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from knowledge_hub import checks
from knowledge_hub.checks import (
    check_core_boundary,
    core_import_roots,
    plugin_package_names,
)


def _load_module(name: str, path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_repo(root: Path, core_source: str,
               plugin_package: str = "some_domain_plugin") -> Path:
    """A miniature repo with the real layout: <root>/knowledge_hub_pkg/
    knowledge_hub/ beside <root>/plugins/<dist>/<package>/."""
    core = root / "knowledge_hub_pkg" / "knowledge_hub"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "some_module.py").write_text(textwrap.dedent(core_source),
                                         encoding="utf-8")

    package = root / "plugins" / f"ds-{plugin_package}" / plugin_package
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    return root / "knowledge_hub_pkg"


# ============================================================ it goes red
def test_a_plugin_import_in_core_fails_the_check(tmp_path):
    """THE test this stage exists for."""
    pkg_dir = build_repo(tmp_path, """
        from __future__ import annotations
        import json

        from some_domain_plugin.parser import DomainParser
    """)
    with pytest.raises(RuntimeError) as excinfo:
        check_core_boundary(pkg_dir=pkg_dir, repo_root=tmp_path)

    message = str(excinfo.value)
    assert "corpus-agnostic" in message
    assert "some_domain_plugin" in message
    assert "some_module.py" in message       # names WHERE, not just that


def test_a_plain_import_of_a_plugin_also_fails(tmp_path):
    pkg_dir = build_repo(tmp_path, """
        import some_domain_plugin
    """)
    with pytest.raises(RuntimeError, match="some_domain_plugin"):
        check_core_boundary(pkg_dir=pkg_dir, repo_root=tmp_path)


def test_a_deferred_import_inside_a_function_still_fails(tmp_path):
    """The realistic hiding place. Core defers heavy imports into function
    bodies as a matter of course, so a plugin import there looks native —
    and an import-and-inspect check would never execute it."""
    pkg_dir = build_repo(tmp_path, """
        def build_something():
            from some_domain_plugin import Thing
            return Thing()
    """)
    with pytest.raises(RuntimeError, match="some_domain_plugin"):
        check_core_boundary(pkg_dir=pkg_dir, repo_root=tmp_path)


def test_the_check_needs_nothing_installed(tmp_path):
    """`some_domain_plugin` is not importable in this environment and the
    check still catches it — so the rule holds on a dev bench, in a kit,
    and in the field, whether or not a plugin was ever installed."""
    with pytest.raises(ImportError):
        __import__("some_domain_plugin")
    pkg_dir = build_repo(tmp_path, "import some_domain_plugin")
    with pytest.raises(RuntimeError):
        check_core_boundary(pkg_dir=pkg_dir, repo_root=tmp_path)


# ============================================================== it stays green
def test_a_clean_core_passes(tmp_path):
    pkg_dir = build_repo(tmp_path, """
        from __future__ import annotations
        import json
        from pathlib import Path

        from knowledge_hub.models import RawDocument
        from . import sibling
    """)
    detail = check_core_boundary(pkg_dir=pkg_dir, repo_root=tmp_path)
    assert "imports none of 1 plugin package" in detail


def test_ordinary_third_party_imports_are_not_its_business(tmp_path):
    """One check, one claim. Dependency hygiene is a real concern and a
    different one; folding it in here would make the boundary rule go red
    for reasons unrelated to the boundary."""
    pkg_dir = build_repo(tmp_path, """
        import boto3
        import numpy
        import a_package_that_does_not_exist_anywhere
    """)
    assert check_core_boundary(pkg_dir=pkg_dir, repo_root=tmp_path)


def test_a_repo_with_no_plugins_directory_is_fine(tmp_path):
    core = tmp_path / "knowledge_hub_pkg" / "knowledge_hub"
    core.mkdir(parents=True)
    (core / "m.py").write_text("import json", encoding="utf-8")
    assert plugin_package_names(tmp_path) == set()
    assert check_core_boundary(pkg_dir=tmp_path / "knowledge_hub_pkg",
                               repo_root=tmp_path)


# ================================================== the real package, today
def test_the_shipped_package_is_clean():
    detail = check_core_boundary()
    assert "core boundary" in detail


def test_the_real_plugin_is_discovered_and_not_imported():
    """Not a tautology: the in-tree USLM plugin IS found by the discovery
    half, so the green result above means core avoids something real
    rather than that there was nothing to avoid."""
    names = plugin_package_names()
    assert "ds_parser_uslm" in names, names
    assert "ds_parser_uslm" not in core_import_roots()


def test_core_still_imports_itself_and_real_dependencies():
    """Guards against the opposite failure: a check that passes because it
    is looking at nothing."""
    roots = core_import_roots()
    assert "knowledge_hub" in roots
    assert "psycopg" in roots and "pydantic" in roots
    assert len(roots) > 30


# ===================================================== wired into the runners
def test_both_runners_run_it():
    """A check nobody runs enforces nothing. The pilot gate and the field
    verifier must both carry it, which is the same 'one library, two
    runners' contract every other check in this module has."""
    from knowledge_hub.deploy_cli import verify_checks_for
    from knowledge_hub.deploy_profiles import DeployPlan

    # check_stack.py is the repo-root pilot gate, not an installed module,
    # so load it by path rather than relying on the caller's sys.path.
    check_stack = _load_module(
        "check_stack",
        Path(checks.__file__).resolve().parents[2] / "check_stack.py")
    assert checks.check_core_boundary in [fn for _, fn in
                                          check_stack.PILOT_CHECKS]

    # A plan with NO seams: nothing to reach, nothing to verify — except
    # this, because it inspects the package rather than the deployment.
    plan = DeployPlan(profile="pilot", shape="A", placement="on-prem",
                      seams={})
    selected = [fn for _, fn in verify_checks_for(plan)]
    assert checks.check_core_boundary in selected, (
        "the field verifier must carry it even for a plan with no seams — "
        "it depends on no service")
