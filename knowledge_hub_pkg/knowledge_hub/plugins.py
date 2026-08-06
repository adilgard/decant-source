"""Component selection by source config — the corpus-agnostic seam.

Two pipeline components used to be hard-wired: `ProcessingService` always
built a `DoclingParser`, and `ExtractionService` chose between exactly two
strategies with an `if` on `data_track`. Both are now REGISTERED and
SELECTED, and the selection is a value on the source's registry config.

That is the whole boundary rule in one sentence: knowledge_hub_pkg owns the
MECHANISM (how a component is named, found, built, and validated) and never
the DOMAIN (which component a given corpus needs). Domain lives in three
places and no others:

  1. ontology sets            — the vocabulary facts must conform to;
  2. source / console config  — which registered component this source uses;
  3. external plugins         — the code those config values point at.

WHAT A CONFIG LOOKS LIKE. Nothing here is corpus-aware; these are just
strings a human or the console wrote onto a source_registry row:

    {"data_track": "prose",
     "extraction_strategy": "parser_supplied",
     "fact_parser": "some_external_pkg.parser:SomeParser",
     "parser": "some_external_pkg.parser:SomeParser"}

A source with none of these behaves exactly as it did before this module
existed. That is deliberate: the seam is opt-in, and the default path is
byte-for-byte the old one.

TWO WAYS TO NAME A COMPONENT, one mechanism:
  * a REGISTERED SHORT NAME ('docling', 'llm', ...) — what core ships and
    what tests register;
  * a DOTTED REFERENCE 'package.module:Attribute' — resolved with importlib
    at selection time. This is how an external plugin is reached, and it is
    why core never imports a plugin: the module name arrives as DATA, from
    a database row, at runtime.

THE IMPORT GUARD. A dotted reference into `knowledge_hub` itself is
REFUSED (see `_ENFORCED_CORE_PREFIX`). A plugin is where domain logic is
allowed to live, so a "plugin" inside core would be precisely the boundary
violation this module exists to prevent. Refusing it at resolution time
makes the rule mechanical instead of remembered, and it does so at the one
choke point every plugin must pass through.

NOT A GENERAL EXTENSION SYSTEM. There are no hooks, no lifecycle, no
ordering, no plugin-to-plugin calls. A plugin implements one of two ABCs
(`Parser`, `FactParser`) and is handed one document at a time. Everything
a plugin returns is validated by core before it reaches storage — most
importantly, a fact parser's output is checked against the ACTIVE ontology
allowlist exactly like an LLM's, by `extraction_parser_supplied.py`. A
plugin is a producer, never a bypass.
"""
from __future__ import annotations

import importlib
import logging
import re
from typing import Any, Callable, Optional

from knowledge_hub.interfaces import FactParser, Parser
from knowledge_hub.models import PROSE_TRACK, RawDocument

logger = logging.getLogger(__name__)

# --------------------------------------------------------------- config keys
# The source-config keys this seam reads. Named as constants because they
# are a contract with the operator console and with whoever hand-writes a
# manifest, not incidental strings.
PARSER_KEY = "parser"
STRATEGY_KEY = "extraction_strategy"
FACT_PARSER_KEY = "fact_parser"
# d.s Stage 5: which SERVED model reads this source's prose. Only consulted
# when the source's strategy is 'llm'; extraction.py routes it through the
# injected llm_strategy_factory, and the console validates it against the
# inference box's live list at save time. Absent = the deployment default
# (settings.extraction_model).
MODEL_KEY = "extraction_model"

# ------------------------------------------------------------ strategy names
# The extraction strategies core ships. 'llm' and 'structured_map' are what
# the data_track branch used to choose between implicitly; naming them makes
# that choice statable in config instead of only inferable from content
# shape. 'parser_supplied' is the new one.
LLM_STRATEGY = "llm"
STRUCTURED_STRATEGY = "structured_map"
PARSER_SUPPLIED_STRATEGY = "parser_supplied"
EXTRACTION_STRATEGIES = (LLM_STRATEGY, STRUCTURED_STRATEGY,
                         PARSER_SUPPLIED_STRATEGY)

# Dotted reference grammar: 'package.module:Attribute'.
_REF = re.compile(r"^(?P<module>[A-Za-z_][A-Za-z0-9_.]*):"
                  r"(?P<attr>[A-Za-z_][A-Za-z0-9_]*)$")

# A plugin reference may not point back into core (see the module docstring).
_ENFORCED_CORE_PREFIX = "knowledge_hub"


class PluginError(Exception):
    """A component named in config could not be produced. Carries WHAT was
    named and WHY it failed, so an operator sees the typo rather than a
    stack trace from three layers down."""

    def __init__(self, kind: str, ref: str, detail: str):
        self.kind, self.ref = kind, ref
        super().__init__(f"{kind} {ref!r}: {detail}")


class BoundaryViolation(PluginError):
    """A plugin reference pointed inside knowledge_hub. Domain logic belongs
    in an external plugin; a plugin living in core is the thing the
    corpus-agnostic rule forbids, so this is refused rather than warned
    about."""


# ---------------------------------------------------------------- registry --
class PluginRegistry:
    """Name -> zero-argument factory, plus dotted-reference loading.

    Factories, not instances: a component is built when a source selects it
    and is cached by the caller against whatever key makes sense there (the
    ontology version, for strategies). This registry itself holds no
    per-tenant or per-document state and is safe to share.
    """

    def __init__(self, kind: str, expected_type: type):
        self.kind = kind
        self._expected = expected_type
        self._factories: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, factory: Callable[..., Any]) -> None:
        """Register a short name. Re-registering the same name REPLACES it,
        which is what a test double needs; core registers its builtins once
        at import."""
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        """Remove a short name. The registries are process-global, so a
        registration made for one test run's benefit outlives it unless
        removed — and other tests assert on exactly what `names()` returns.
        Absent names are ignored: teardown must not fail because setup
        never ran."""
        self._factories.pop(name, None)

    def names(self) -> list[str]:
        """Registered short names, sorted. The operator console reads this
        to populate a picker, so it must never include a dotted reference —
        those are typed, not chosen."""
        return sorted(self._factories)

    def build(self, ref: str, **kwargs: Any) -> Any:
        """Produce the component `ref` names. Short name first (cheap, no
        import), then a dotted reference. The result is type-checked against
        the ABC this registry is for, so a config typo pointing at some
        unrelated class fails HERE with a clear message instead of at the
        first method call."""
        if not isinstance(ref, str) or not ref.strip():
            raise PluginError(self.kind, str(ref), "must be a non-empty string")
        ref = ref.strip()
        factory = self._factories.get(ref)
        if factory is None:
            factory = self._load_ref(ref)
        try:
            component = factory(**kwargs)
        except TypeError as e:
            raise PluginError(
                self.kind, ref,
                f"could not be constructed with {sorted(kwargs)}: {e}") from e
        if not isinstance(component, self._expected):
            raise PluginError(
                self.kind, ref,
                f"produced a {type(component).__name__}, which does not "
                f"implement {self._expected.__name__}")
        return component

    # ----------------------------------------------------------- internals --
    def _load_ref(self, ref: str) -> Callable[..., Any]:
        match = _REF.match(ref)
        if match is None:
            raise PluginError(
                self.kind, ref,
                f"is neither a registered name ({', '.join(self.names()) or 'none'}) "
                f"nor a 'package.module:Attribute' reference")
        module_name, attr = match.group("module"), match.group("attr")
        root = module_name.split(".", 1)[0]
        if root == _ENFORCED_CORE_PREFIX:
            raise BoundaryViolation(
                self.kind, ref,
                f"points inside {_ENFORCED_CORE_PREFIX}, which is "
                f"corpus-agnostic by contract. A plugin is where "
                f"domain-specific parsing belongs, so it must live in its "
                f"own package outside the core one")
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            raise PluginError(
                self.kind, ref,
                f"module {module_name!r} is not importable — is the plugin "
                f"package installed in this environment? ({e})") from e
        try:
            return getattr(module, attr)
        except AttributeError as e:
            raise PluginError(
                self.kind, ref,
                f"module {module_name!r} has no attribute {attr!r}") from e


# The two registries core exposes. Strategies are NOT a registry: they need
# an ontology binding and are built by ExtractionService against the
# version a document is pinned to, so a name-to-factory table would just be
# a second place for that wiring to drift.
PARSERS = PluginRegistry("parser", Parser)
FACT_PARSERS = PluginRegistry("fact_parser", FactParser)


def _docling_parser() -> Parser:
    # Imported lazily: Docling pulls in a heavy model stack, and a
    # deployment whose sources all use plugin parsers should not pay for it
    # at import time.
    from knowledge_hub.parsing_docling import DoclingParser
    return DoclingParser()


DEFAULT_PARSER = "docling"
PARSERS.register(DEFAULT_PARSER, _docling_parser)


# ------------------------------------------------------------ source config --
def source_config(store, raw: RawDocument) -> dict[str, Any]:
    """The effective config for the document `raw` belongs to.

    Same precedence rule the pipeline already uses in two other places (the
    data_track declaration in processing, the structured_map in extraction):
    a per-item `native_metadata` value wins, the source's registry row fills
    the gaps. Repeating the rule rather than inventing a third one is the
    point — an operator should not have to remember which knob overrides
    which depending on where they set it.
    """
    native = dict(raw.native_metadata or {})
    source_ref = native.get("source_ref")
    config: dict[str, Any] = {}
    if source_ref:
        with store.transaction(raw.tenant_id) as conn:
            row = conn.execute(
                "SELECT config FROM source_registry"
                " WHERE tenant_id = %s AND source_ref = %s",
                (raw.tenant_id, source_ref)).fetchone()
        config = dict(((row or {}).get("config") or {}))
    config.update({k: v for k, v in native.items() if v is not None})
    return config


def parser_ref_for(config: dict[str, Any]) -> str:
    """Which Parser this source wants. Absent means the shipped default, so
    every source that predates this seam keeps its exact behavior."""
    return str(config.get(PARSER_KEY) or DEFAULT_PARSER)


def strategy_name_for(config: dict[str, Any], data_track: str) -> str:
    """Which extraction strategy this source wants.

    Absent means the historical routing: prose to the LLM, everything else
    to the deterministic column map. `data_track` therefore keeps its
    original meaning (what SHAPE the content is, which drives parsing and
    chunking) and does not quietly become a second name for "who produces
    the facts". Those are different questions and a source can now answer
    them independently.
    """
    declared = config.get(STRATEGY_KEY)
    if declared is None:
        return LLM_STRATEGY if data_track == PROSE_TRACK else STRUCTURED_STRATEGY
    name = str(declared)
    if name not in EXTRACTION_STRATEGIES:
        raise PluginError(
            "extraction_strategy", name,
            f"is not one of {', '.join(EXTRACTION_STRATEGIES)}")
    return name


def fact_parser_ref_for(config: dict[str, Any]) -> Optional[str]:
    """Which fact-parser plugin a parser_supplied source uses. None when the
    key is absent, which the strategy turns into a named error rather than
    silently producing nothing."""
    ref = config.get(FACT_PARSER_KEY)
    return str(ref) if ref else None


def build_fact_parser(ref: str) -> FactParser:
    """Resolve and construct a fact-parser plugin. Thin, but it is the ONE
    place a plugin enters the process, which makes it the one place to audit
    and the one place the boundary guard has to hold."""
    parser = FACT_PARSERS.build(ref)
    logger.info("fact parser %r resolved to %s v%s", ref,
                type(parser).__name__, parser.version)
    return parser
