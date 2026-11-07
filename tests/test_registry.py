import pytest

from ontology.errors import OntologyError
from ontology.registry import build_registry, load_seed, parse_type


def test_seed_loads_and_references_resolve():
    reg = build_registry(load_seed())
    assert {"Asset", "Narrative", "Event", "Entity", "Concept"} <= set(reg.object_types)
    assert reg.link_type("affects").allows("Event", "Narrative")
    assert not reg.link_type("affects").allows("Narrative", "Event")


def test_meaningless_link_names_are_refused():
    with pytest.raises(OntologyError, match="says nothing"):
        parse_type("link", "related_to", {"description": "x", "from": ["Asset"], "to": ["Asset"]})


def test_link_to_unknown_type_fails_registry_build():
    bad = parse_type("link", "hits", {"description": "x", "from": ["Asset"], "to": ["Planet"]})
    with pytest.raises(OntologyError, match="unknown object type 'Planet'"):
        build_registry([*load_seed(), bad])


def test_props_are_typed():
    asset = build_registry(load_seed()).object_type("Asset")
    assert asset.validate_props({"symbol": "BTC", "asset_class": "spot"}) == {"symbol": "BTC", "asset_class": "spot"}
    with pytest.raises(OntologyError, match="one of"):
        asset.validate_props({"symbol": "BTC", "asset_class": "meme"})
    with pytest.raises(OntologyError, match="no properties"):
        asset.validate_props({"symbol": "BTC", "colour": "orange"})
    with pytest.raises(OntologyError, match="requires"):
        asset.validate_props({})
