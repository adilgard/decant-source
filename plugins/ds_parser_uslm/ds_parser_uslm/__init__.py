"""USLM (US Code XML) parser plugin for decant.Source.

Selected by source config, never imported by core:

    {"data_track": "prose",
     "parser": "ds_parser_uslm.parser:UslmParser",
     "extraction_strategy": "parser_supplied",
     "fact_parser": "ds_parser_uslm.parser:UslmParser"}

The same class fills both roles so one parse produces the text AND the fact
offsets — the only arrangement where a citation is guaranteed to point at
the words the chunker actually stored.

Requires the vocabulary in `ontology/tax-statute-0.1.json` to be imported
and active. Importing it is an operator action; this package only declares
what it emits. Core validates against whatever is ACTIVE and quarantines
the rest, so a mismatch is visible rather than silent.
"""
from ds_parser_uslm.parser import (
    EMITTED_ENTITY_TYPES,
    EMITTED_PREDICATES,
    IDENTIFIER_KEY,
    UslmParser,
)

__version__ = "0.1.1"

__all__ = ["UslmParser", "EMITTED_ENTITY_TYPES", "EMITTED_PREDICATES",
           "IDENTIFIER_KEY", "__version__"]
