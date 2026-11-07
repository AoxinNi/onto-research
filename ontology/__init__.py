"""Typed ontology core for the BTC multi-agent company."""

from .errors import OntologyError
from .store import OntologyStore, Provenance

__all__ = ["OntologyError", "OntologyStore", "Provenance"]
