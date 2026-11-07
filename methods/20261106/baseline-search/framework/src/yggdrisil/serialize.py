from __future__ import annotations

import hashlib
import json
import math
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any, TypeVar

from yggdrisil.exceptions import SerializationError

_TAG = "__yggdrisil__"
_REGISTERED_TYPES: dict[str, type[Any]] = {}

T = TypeVar("T", bound=type[Any])


def serializable(cls: T) -> T:
    """Register a state or action type that may be restored from storage.

    Registration is explicit so a database cannot choose a module to import.
    Decorate application dataclasses or Pydantic models that are stored in a
    graph. Built-in containers and scalar values need no registration.
    """

    _REGISTERED_TYPES[_qualname(cls)] = cls
    return cls


def stable_hash(data: Any) -> str:
    """SHA-256 of a canonical encoding. Same logical value -> same digest."""

    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def canonical_json(data: Any) -> str:
    return json.dumps(
        to_tagged(data),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def dumps(data: Any) -> str:
    return canonical_json(data)


def loads(text: str) -> Any:
    return from_tagged(json.loads(text))


def to_tagged(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return {_TAG: "float", "value": str(obj)}
        return obj
    if isinstance(obj, datetime):
        return {_TAG: "datetime", "iso": obj.isoformat()}
    if isinstance(obj, frozenset):
        return {_TAG: "frozenset", "items": _sorted_tagged(obj)}
    if isinstance(obj, set):
        return {_TAG: "set", "items": _sorted_tagged(obj)}
    if isinstance(obj, tuple):
        return {_TAG: "tuple", "items": [to_tagged(x) for x in obj]}
    if isinstance(obj, list):
        return [to_tagged(x) for x in obj]
    if isinstance(obj, dict):
        items = [[to_tagged(key), to_tagged(value)] for key, value in obj.items()]
        items.sort(key=lambda item: _canonical_tagged(item[0]))
        return {_TAG: "dict", "items": items}
    if is_dataclass(obj) and not isinstance(obj, type):
        qualname = _registered_name(type(obj))
        return {
            _TAG: "object",
            "kind": "dataclass",
            "qualname": qualname,
            "fields": [
                [field.name, to_tagged(getattr(obj, field.name))]
                for field in fields(obj)
            ],
        }
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        qualname = _registered_name(type(obj))
        values = dump(mode="python")
        return {
            _TAG: "object",
            "kind": "pydantic",
            "qualname": qualname,
            "fields": [[name, to_tagged(value)] for name, value in values.items()],
        }
    raise SerializationError(f"cannot serialize {type(obj).__name__}")


def from_tagged(obj: Any) -> Any:
    if isinstance(obj, list):
        return [from_tagged(x) for x in obj]
    if not isinstance(obj, dict) or _TAG not in obj:
        if isinstance(obj, dict):
            if "__type__" in obj:
                raise SerializationError(
                    "legacy serialized value detected; pre-0.1 graph files "
                    "cannot be resumed safely and must be migrated or rebuilt"
                )
            return {key: from_tagged(value) for key, value in obj.items()}
        return obj

    tag = obj[_TAG]
    if tag == "float":
        return float(obj["value"])
    if tag == "datetime":
        return datetime.fromisoformat(obj["iso"])
    if tag == "frozenset":
        return frozenset(from_tagged(x) for x in obj["items"])
    if tag == "set":
        return {from_tagged(x) for x in obj["items"]}
    if tag == "tuple":
        return tuple(from_tagged(x) for x in obj["items"])
    if tag == "dict":
        return {from_tagged(key): from_tagged(value) for key, value in obj["items"]}
    if tag == "object":
        cls = _REGISTERED_TYPES.get(obj["qualname"])
        if cls is None:
            raise SerializationError(
                f"unregistered serialized type: {obj['qualname']}; "
                "decorate it with @serializable before opening the graph"
            )
        values = {name: from_tagged(value) for name, value in obj["fields"]}
        return cls(**values)
    raise SerializationError(f"unknown serialization tag: {tag}")


def _sorted_tagged(items: Any) -> list[Any]:
    tagged = [to_tagged(x) for x in items]
    tagged.sort(key=_canonical_tagged)
    return tagged


def _canonical_tagged(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _registered_name(cls: type[Any]) -> str:
    qualname = _qualname(cls)
    if _REGISTERED_TYPES.get(qualname) is not cls:
        raise SerializationError(
            f"{qualname} is not registered; decorate it with @serializable"
        )
    return qualname


def _qualname(cls: type[Any]) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"
