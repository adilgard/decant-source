"""Ontology import + selection (d.s Stage 1).

What must hold:

* validation is deterministic and SPECIFIC — every malformed set is rejected
  with an error naming exactly what is wrong; nothing is silently repaired;
* the portable file and the loaded row are both idempotent on identical
  content, and a version is IMMUTABLE — same version + different content is
  refused everywhere, never overwritten;
* importing is INERT: the active selection does not move on import;
* selecting is the act: get_ontology_definition's unpinned path (and
  therefore every unpinned binding) follows the single ontology_active row,
  and ONLY that row — inserting a newer version no longer changes the answer
  (the old newest-effective_from rule is dead as a control path);
* the operator ops travel the audited write gate: reviewer role is refused
  (deny-by-default) and the refusal is audited; a validation failure is a
  WriteCallError (HTTP 400), not a 500.

Isolation note: ontology_versions / ontology_active are GLOBAL tables (no
tenant column) in the shared session database, so every test that moves the
selection restores baseline-0.1 — other tests' unpinned reads must keep
resolving the seed.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from factories import ONTOLOGY

from knowledge_hub.config import settings
from knowledge_hub.ontology import PostgresOntologyBinding
from knowledge_hub.ontology_registry import (
    OntologyValidationError,
    load_ontology_file,
    save_ontology_file,
    validate_ontology_set,
)
from knowledge_hub.operator_http import (
    OperatorGate,
    OperatorService,
    WriteCallError,
    WriteRefused,
    register_operator_defaults,
)
from knowledge_hub.serving import Principal


def fresh_set(**overrides):
    """A minimal valid ontology set with a collision-proof version."""
    data = {
        "version": f"t-{uuid.uuid4().hex[:10]}",
        "entity_types": ["Vendor", "Customer"],
        "predicates": ["supplies", "invoiced_by"],
    }
    data.update(overrides)
    return data


@pytest.fixture()
def keep_baseline_active(store, tenant):
    """Whatever a test does to the GLOBAL selection, put the seed back."""
    yield
    store.set_active_ontology(tenant, ONTOLOGY)


# ---------------------------------------------------------------------------
# Validation — deterministic, specific, nothing repaired silently.
# ---------------------------------------------------------------------------

def test_validate_accepts_minimal_set():
    onto = validate_ontology_set(fresh_set())
    assert onto.definition == {"entity_types": ["Vendor", "Customer"],
                               "predicates": ["supplies", "invoiced_by"]}
    assert onto.notes is None


def test_validate_accepts_the_shipped_baseline_file():
    """The portable form of the seeded ontology passes its own gate — the
    folder's contract and the schema seed can't drift apart unnoticed."""
    path = Path(__file__).resolve().parents[2] / "ontologies" / "baseline-0.1.json"
    onto = load_ontology_file(path)
    assert onto.version == ONTOLOGY
    assert "examples" in onto.definition
    assert "predicate_aliases" in onto.definition


@pytest.mark.parametrize("mutate, fragment", [
    (lambda d: d.update(version=""), "version must be a non-empty string"),
    (lambda d: d.update(version="../evil"), "must match"),
    (lambda d: d.update(version=42), "version must be a non-empty string"),
    (lambda d: d.pop("entity_types"), "missing required key"),
    (lambda d: d.pop("predicates"), "missing required key"),
    (lambda d: d.update(entity_types=[]), "must not be empty"),
    (lambda d: d.update(entity_types="Vendor"), "must be a JSON array"),
    (lambda d: d.update(entity_types=["Vendor", 7]), "only strings"),
    (lambda d: d.update(entity_types=["Vendor", "  "]), "blank entries"),
    (lambda d: d.update(entity_types=["Vendor", "Vendor"]), "duplicates"),
    (lambda d: d.update(predicates=["Supplies By"]), "lowercase_underscore"),
    (lambda d: d.update(schema={"Vendor": {}}), "unknown key"),
    (lambda d: d.update(examples={"predicates": {"nope": ["x"]}}),
     "not declared"),
    (lambda d: d.update(examples={"entity_types": {"Vendor": []}}),
     "non-empty list"),
    (lambda d: d.update(predicate_aliases={"supplied_by": "nope"}),
     "not a declared predicate"),
    (lambda d: d.update(predicate_aliases={"supplies": "supplies"}),
     "collides with a declared predicate"),
    (lambda d: d.update(predicate_aliases={"Supplied By": "supplies"}),
     None),  # canon('Supplied By') = supplied_by — variant form is FINE
])
def test_validate_rejections_are_specific(mutate, fragment):
    data = fresh_set()
    mutate(data)
    if fragment is None:
        validate_ontology_set(data)  # the counter-example: must pass
        return
    with pytest.raises(OntologyValidationError, match=fragment):
        validate_ontology_set(data)


def test_validate_alias_object_form():
    good = fresh_set(predicate_aliases={
        "supplied_by": {"predicate": "supplies", "swap": True}})
    assert "predicate_aliases" in validate_ontology_set(good).definition
    with pytest.raises(OntologyValidationError, match="unknown key"):
        validate_ontology_set(fresh_set(predicate_aliases={
            "supplied_by": {"predicate": "supplies", "flip": True}}))
    with pytest.raises(OntologyValidationError, match="swap must be"):
        validate_ontology_set(fresh_set(predicate_aliases={
            "supplied_by": {"predicate": "supplies", "swap": "yes"}}))


# ---------------------------------------------------------------------------
# Portable files — atomic, idempotent, immutable.
# ---------------------------------------------------------------------------

def test_save_is_idempotent_and_immutable(tmp_path):
    onto = validate_ontology_set(fresh_set(notes="round one"))
    path = save_ontology_file(onto, folder=tmp_path)
    assert path == tmp_path / f"{onto.version}.json"
    assert load_ontology_file(path).definition == onto.definition
    assert save_ontology_file(onto, folder=tmp_path) == path  # no-op re-save

    changed = validate_ontology_set({
        "version": onto.version,
        "entity_types": ["Vendor"], "predicates": ["supplies"]})
    with pytest.raises(OntologyValidationError, match="immutable"):
        save_ontology_file(changed, folder=tmp_path)
    # The refused write must not have clobbered the original.
    assert load_ontology_file(path).definition == onto.definition


# ---------------------------------------------------------------------------
# The loaded form + the selection — one source of truth.
# ---------------------------------------------------------------------------

def test_migration_seeded_the_selection(store, tenant):
    versions = store.list_ontology_versions(tenant)
    active = [v for v in versions if v["active"]]
    assert [v["version"] for v in active] == [ONTOLOGY]


def test_insert_is_inert_and_immutable(store, tenant):
    onto = validate_ontology_set(fresh_set())
    assert store.insert_ontology_version(
        tenant, onto.version, onto.definition) == "created"
    # Idempotent on identical content.
    assert store.insert_ontology_version(
        tenant, onto.version, onto.definition) == "already_imported"
    # Immutable under different content.
    with pytest.raises(ValueError, match="immutable"):
        store.insert_ontology_version(
            tenant, onto.version, {"entity_types": ["X"], "predicates": ["y"]})
    # INERT: the selection did not move, even though the new row's
    # effective_from is newer — insertion is no longer activation.
    version, _ = store.get_ontology_definition(tenant)
    assert version == ONTOLOGY


def test_selection_is_the_single_source_of_truth(store, tenant,
                                                 keep_baseline_active):
    onto = validate_ontology_set(fresh_set())
    store.insert_ontology_version(tenant, onto.version, onto.definition)
    store.set_active_ontology(tenant, onto.version, activated_by="op-test")

    # The unpinned store read, an unpinned binding, and the listing all
    # agree; the explicit-version path still answers for any version.
    assert store.get_ontology_definition(tenant)[0] == onto.version
    binding = PostgresOntologyBinding(store)
    assert binding.version == onto.version
    assert binding.is_predicate("supplies")
    assert not binding.is_predicate("authored_by")   # baseline vocabulary
    assert store.get_ontology_definition(tenant, ONTOLOGY)[0] == ONTOLOGY
    listing = store.list_ontology_versions(tenant)
    assert {v["version"] for v in listing if v["active"]} == {onto.version}

    # Swapping back is the same one-row act.
    store.set_active_ontology(tenant, ONTOLOGY)
    assert PostgresOntologyBinding(store).version == ONTOLOGY


def test_select_unknown_version_is_a_lookup_error(store, tenant):
    with pytest.raises(LookupError, match="import it"):
        store.set_active_ontology(tenant, "never-imported-9.9")


# ---------------------------------------------------------------------------
# The operator gate — audited, role-scoped, validation surfaces as 400.
# ---------------------------------------------------------------------------

@pytest.fixture()
def gate_and_service(store, tmp_path, monkeypatch):
    # The portable folder goes to a scratch dir; the handlers read the
    # setting at call time, so this redirects the file half of import.
    monkeypatch.setattr(settings, "ontology_dir", str(tmp_path))
    from knowledge_hub.capture import SourceRegistry
    service = OperatorService(store, resolution=None,
                              registry=SourceRegistry(store))
    gate = OperatorGate(store)
    register_operator_defaults(gate, service)
    return gate, service


def audit_rows(store, tenant, action):
    with store.transaction(tenant) as conn:
        return conn.execute(
            "SELECT action, outcome, target FROM operator_audit"
            " WHERE tenant_id = %s AND action = %s ORDER BY id",
            (tenant, action)).fetchall()


def test_operator_can_import_and_select_and_it_is_audited(
        gate_and_service, store, tenant, tmp_path, keep_baseline_active):
    gate, service = gate_and_service
    operator = Principal(tenant_id=tenant, principal_id="op-test",
                         roles=["operator"])
    data = fresh_set()

    out = gate.execute("import_ontology", {"ontology": data}, operator)
    assert out["result"]["status"] == "created"
    assert (tmp_path / f"{data['version']}.json").is_file()  # portable form
    assert store.get_ontology_definition(tenant)[0] == ONTOLOGY  # inert

    out = gate.execute("select_ontology", {"version": data["version"]},
                       operator)
    assert out["result"]["active_version"] == data["version"]
    assert "future ingests only" in out["result"]["applies_to"]
    assert store.get_ontology_definition(tenant)[0] == data["version"]

    assert [(r["outcome"], r["target"]) for r in
            audit_rows(store, tenant, "import_ontology")] == \
        [("applied", f"ontology:{data['version']}")]
    assert [(r["outcome"], r["target"]) for r in
            audit_rows(store, tenant, "select_ontology")] == \
        [("applied", f"ontology:{data['version']}")]

    # The console listing the UI renders.
    listing = service.list_ontologies(tenant)
    assert listing["active"] == data["version"]
    assert any(v["version"] == data["version"] and v["active"]
               for v in listing["versions"])


def test_reviewer_is_refused_and_the_refusal_is_audited(
        gate_and_service, store, tenant):
    gate, _ = gate_and_service
    reviewer = Principal(tenant_id=tenant, principal_id="rv-test",
                         roles=["reviewer"])
    for action, params in (("import_ontology", {"ontology": fresh_set()}),
                           ("select_ontology", {"version": ONTOLOGY})):
        with pytest.raises(WriteRefused):
            gate.execute(action, params, reviewer)
        assert [r["outcome"] for r in audit_rows(store, tenant, action)] == \
            ["refused"]
    # Deny-by-default proven: nothing moved.
    assert store.get_ontology_definition(tenant)[0] == ONTOLOGY


def test_invalid_set_is_a_write_call_error(gate_and_service, tenant):
    gate, _ = gate_and_service
    operator = Principal(tenant_id=tenant, principal_id="op-test",
                         roles=["operator"])
    bad = fresh_set(predicates=["Supplies By"])   # non-canonical form
    with pytest.raises(WriteCallError, match="lowercase_underscore"):
        gate.execute("import_ontology", {"ontology": bad}, operator)
