"""Type registry: the designed schema that every write is checked against."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .errors import OntologyError

SEED_PATH = Path(__file__).parent / "schema" / "types.yaml"

PropType = Literal["string", "text", "number", "integer", "boolean", "datetime", "enum", "string_list"]

# Relationship names that carry no meaning; a typed ontology refuses them.
BANNED_LINK_NAMES = {"related_to", "related", "relates_to", "associated_with", "linked_to", "connected_to"}

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")


class PropertySpec(BaseModel):
    type: PropType
    required: bool = False
    values: list[str] | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _enum_needs_values(self) -> PropertySpec:
        if self.type == "enum" and not self.values:
            raise ValueError("enum property needs non-empty 'values'")
        return self

    def coerce(self, name: str, value: Any) -> Any:
        t = self.type
        if t in ("string", "text"):
            if not isinstance(value, str):
                raise OntologyError(f"property '{name}' must be a string")
            return value
        if t == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OntologyError(f"property '{name}' must be a number")
            return float(value)
        if t == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise OntologyError(f"property '{name}' must be an integer")
            return value
        if t == "boolean":
            if not isinstance(value, bool):
                raise OntologyError(f"property '{name}' must be true or false")
            return value
        if t == "datetime":
            try:
                return datetime.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise OntologyError(f"property '{name}' must be an ISO 8601 datetime") from None
        if t == "enum":
            if value not in (self.values or []):
                raise OntologyError(f"property '{name}' must be one of {self.values}, got {value!r}")
            return value
        if t == "string_list":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise OntologyError(f"property '{name}' must be a list of strings")
            return value
        raise OntologyError(f"unknown property type {t}")  # pragma: no cover


class _TypeBase(BaseModel):
    name: str
    description: str
    properties: dict[str, PropertySpec] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("type name must be ASCII letters/digits/underscore, starting with a letter")
        return v

    def validate_props(self, props: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
        unknown = set(props) - set(self.properties)
        if unknown:
            allowed = sorted(self.properties) or "none"
            raise OntologyError(f"{self.name} has no properties {sorted(unknown)}; allowed: {allowed}")
        out = {k: self.properties[k].coerce(k, v) for k, v in props.items() if v is not None}
        if not partial:
            missing = [k for k, spec in self.properties.items() if spec.required and k not in out]
            if missing:
                raise OntologyError(f"{self.name} requires properties {missing}")
        return out


class ObjectType(_TypeBase):
    kind: Literal["object"] = "object"
    layer: Literal["world", "company"]


class LinkType(_TypeBase):
    kind: Literal["link"] = "link"
    from_types: list[str] = Field(alias="from")
    to_types: list[str] = Field(alias="to")

    model_config = {"populate_by_name": True}

    @field_validator("name")
    @classmethod
    def _not_banned(cls, v: str) -> str:
        if v.lower() in BANNED_LINK_NAMES:
            raise ValueError(f"'{v}' says nothing about the relationship; name what it actually is")
        return v

    def allows(self, from_type: str, to_type: str) -> bool:
        return ("*" in self.from_types or from_type in self.from_types) and (
            "*" in self.to_types or to_type in self.to_types
        )


class Registry:
    def __init__(self, object_types: dict[str, ObjectType], link_types: dict[str, LinkType]):
        self.object_types = object_types
        self.link_types = link_types

    def object_type(self, name: str) -> ObjectType:
        try:
            return self.object_types[name]
        except KeyError:
            raise OntologyError(
                f"unknown object type '{name}'; known: {sorted(self.object_types)}. "
                "Use propose_type if a new type is really needed."
            ) from None

    def link_type(self, name: str) -> LinkType:
        try:
            return self.link_types[name]
        except KeyError:
            raise OntologyError(
                f"unknown link type '{name}'; known: {sorted(self.link_types)}. "
                "Use propose_type if a new type is really needed."
            ) from None

    def check_references(self) -> None:
        """Every link endpoint must name a known object type."""
        for lt in self.link_types.values():
            for t in lt.from_types + lt.to_types:
                if t != "*" and t not in self.object_types:
                    raise OntologyError(f"link type '{lt.name}' refers to unknown object type '{t}'")

    def describe(self) -> dict[str, Any]:
        return {
            "object_types": [t.model_dump(exclude_none=True) for t in self.object_types.values()],
            "link_types": [t.model_dump(by_alias=True, exclude_none=True) for t in self.link_types.values()],
        }


def parse_type(kind: str, name: str, spec: dict[str, Any]) -> ObjectType | LinkType:
    try:
        if kind == "object":
            return ObjectType(name=name, **spec)
        if kind == "link":
            return LinkType(name=name, **spec)
    except ValueError as e:
        raise OntologyError(f"invalid {kind} type '{name}': {e}") from None
    raise OntologyError(f"kind must be 'object' or 'link', got {kind!r}")


def load_seed(path: Path = SEED_PATH) -> list[ObjectType | LinkType]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    types: list[ObjectType | LinkType] = []
    for name, spec in (raw.get("object_types") or {}).items():
        types.append(parse_type("object", name, spec))
    for name, spec in (raw.get("link_types") or {}).items():
        types.append(parse_type("link", name, spec))
    return types


def build_registry(types: list[ObjectType | LinkType]) -> Registry:
    reg = Registry(
        {t.name: t for t in types if isinstance(t, ObjectType)},
        {t.name: t for t in types if isinstance(t, LinkType)},
    )
    reg.check_references()
    return reg
