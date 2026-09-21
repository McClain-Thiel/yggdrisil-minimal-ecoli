"""Evaluate KEGG module completeness with Lark and boolean.py.

Spaces join pathway blocks, commas choose alternatives, + joins required complex
components, and - marks optional components: https://www.kegg.jp/kegg/module.html
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TypeAlias, TypedDict

from boolean import BooleanAlgebra
from boolean import Expression as BooleanExpression
from lark import Lark, Token, Tree, UnexpectedInput
from pandas import DataFrame

from yggdrisil_ecoli.data.errors import DataValidationError

Expression: TypeAlias = Tree[Token] | Token
PARSER_SEMANTICS_VERSION = "1"
_LOGIC = BooleanAlgebra()


class ModuleExpressionError(DataValidationError):
    """A module definition is malformed or cannot be evaluated exactly."""


class KeggModuleEntry(TypedDict):
    name: str
    definition: str
    module_class: str | None


# Whitespace is an operator, not ignored formatting: OR binds inside each block.
_PARSER = Lark(
    r"""
    ?start: alternatives | alternatives (_SPACE alternatives)+ -> conjunction
    ?alternatives: complex | complex ("," complex)+ -> alternatives
    ?complex: primary | primary ("+" primary | optional)+ -> conjunction
    optional: "-" primary
    ?primary: ID | "(" start ")"
    ID: /[KM][0-9]{5}/
    _SPACE: / +/
    """,
    parser="lalr",
)


def parse_module_expression(raw: str) -> Expression:
    try:
        return _PARSER.parse(raw.strip())
    except UnexpectedInput as exc:
        raise ModuleExpressionError(f"invalid module expression: {raw!r}") from exc


def evaluate_module_expression(
    expression: Expression,
    present_kos: set[str] | frozenset[str],
    *,
    module_definitions: dict[str, Expression] | None = None,
) -> bool:
    """Return whether the supplied KOs satisfy the module's Boolean definition."""
    definitions = module_definitions or {}

    def substitute(node: Expression, stack: tuple[str, ...] = ()) -> BooleanExpression:
        if isinstance(node, Token):
            identifier = str(node)
            if identifier.startswith("K"):
                return _LOGIC.TRUE if identifier in present_kos else _LOGIC.FALSE
            if identifier in stack:
                raise ModuleExpressionError(f"cyclic module reference: {identifier}")
            if identifier not in definitions:
                raise ModuleExpressionError(
                    f"unresolved module reference: {identifier}"
                )
            return substitute(definitions[identifier], (*stack, identifier))
        if node.data == "optional":
            return _LOGIC.TRUE
        operation = _LOGIC.OR if node.data == "alternatives" else _LOGIC.AND
        return operation(
            *(substitute(child, stack) for child in node.children)
        ).simplify()

    return bool(substitute(expression) == _LOGIC.TRUE)


def referenced_ids(expression: Expression) -> frozenset[str]:
    """Include optional components when reporting which genes relate to a module."""
    if isinstance(expression, Token):
        return frozenset({str(expression)})
    return frozenset(
        str(token)
        for token in expression.scan_values(lambda item: isinstance(item, Token))
    )


def registry_ko_mapping_hash(registry: DataFrame) -> str:
    payload = [(tag, sorted(kos)) for tag, kos in registry.ko_ids.sort_index().items()]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode()
    ).hexdigest()


def parse_kegg_module_flat_file(path: str | Path) -> dict[str, KeggModuleEntry]:
    return parse_kegg_module_flat_text(Path(path).read_text())


def parse_kegg_module_flat_text(raw: str) -> dict[str, KeggModuleEntry]:
    """Read KEGG's fixed-width fields, including wrapped definitions."""
    entries = {}
    for record in raw.split("///"):
        if not record.strip():
            continue
        fields: dict[str, str] = {}
        field = ""
        for line in record.splitlines():
            if not line.strip():
                continue
            field = line[:12].strip() or field
            if not field:
                raise ModuleExpressionError("continuation without a field")
            fields[field] = (fields.get(field, "") + " " + line[12:].strip()).strip()
        module_id = fields.get("ENTRY", "").split(" ")[0]
        if not re.fullmatch(r"M[0-9]{5}", module_id):
            raise ModuleExpressionError("missing or malformed ENTRY")
        if module_id in entries:
            raise ModuleExpressionError(f"duplicate module entry: {module_id}")
        if not fields.get("NAME") or not fields.get("DEFINITION"):
            raise ModuleExpressionError(f"{module_id}: missing NAME or DEFINITION")
        parse_module_expression(fields["DEFINITION"])
        entries[module_id] = KeggModuleEntry(
            name=fields["NAME"],
            definition=fields["DEFINITION"],
            module_class=fields.get("CLASS"),
        )
    if not entries:
        raise ModuleExpressionError("KEGG module flat file contained no entries")
    return entries
