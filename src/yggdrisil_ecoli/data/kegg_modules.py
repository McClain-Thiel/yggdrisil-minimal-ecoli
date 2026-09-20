"""KEGG block logic and exact, subset-minimal missing-KO explanations.

Spaces join pathway blocks, commas choose alternatives, + joins required complex
components, and - marks optional components: https://www.kegg.jp/kegg/module.html
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import TypeAlias

from boolean import BooleanAlgebra
from boolean import Expression as BooleanExpression
from lark import Lark, Token, Tree, UnexpectedInput

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry

Expression: TypeAlias = Tree[Token] | Token
PARSER_SEMANTICS_VERSION = "1"
_LOGIC = BooleanAlgebra()


class ModuleExpressionError(DataValidationError):
    """A module definition is malformed or cannot be evaluated exactly."""


@dataclass(frozen=True)
class ModuleEvaluation:
    complete: bool
    missing_required_kos: tuple[str, ...]
    minimal_missing_ko_sets: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class KeggModuleEntry:
    module_id: str
    name: str
    definition: str
    module_class: str | None = None

    @property
    def expression(self) -> Expression:
        return parse_module_expression(self.definition)


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
    max_options: int = 4096,
) -> ModuleEvaluation:
    """Find all subset-minimal KO additions; never truncate an exact answer.

    The budget bounds intermediate DNF expansion before distribution, so an
    expression can exceed it even if its final simplified answer is smaller.
    """
    if max_options < 1:
        raise ValueError("max_options must be positive")
    definitions = module_definitions or {}

    def residual(node: Expression, stack: tuple[str, ...] = ()) -> BooleanExpression:
        if isinstance(node, Token):
            identifier = str(node)
            if identifier.startswith("K"):
                return (
                    _LOGIC.TRUE
                    if identifier in present_kos
                    else _LOGIC.Symbol(identifier)
                )
            if identifier in stack:
                raise ModuleExpressionError(f"cyclic module reference: {identifier}")
            if identifier not in definitions:
                raise ModuleExpressionError(
                    f"unresolved module reference: {identifier}"
                )
            return residual(definitions[identifier], (*stack, identifier))
        if node.data == "optional":
            return _LOGIC.TRUE
        operation = _LOGIC.OR if node.data == "alternatives" else _LOGIC.AND
        children = [residual(child, stack) for child in node.children]
        result = children[0]
        for child in children[1:]:
            combined = operation(result, child).simplify()
            if isinstance(combined, _LOGIC.AND):
                candidates = prod(
                    len(arg.args) if isinstance(arg, _LOGIC.OR) else 1
                    for arg in combined.args
                )
                if candidates > max_options:
                    raise ModuleExpressionError(
                        f"intermediate DNF expansion limit exceeded ({max_options})"
                    )
            # The library, rather than a custom set-product implementation,
            # handles Boolean distribution and absorption.
            result = _LOGIC.dnf(combined)
            if isinstance(result, _LOGIC.OR) and len(result.args) > max_options:
                raise ModuleExpressionError(
                    f"module expression exceeds option limit ({max_options})"
                )
        return result

    result = residual(expression)
    if result == _LOGIC.TRUE:
        return ModuleEvaluation(True, (), ())
    terms = result.args if isinstance(result, _LOGIC.OR) else (result,)
    options = [
        frozenset(str(symbol) for symbol in term.get_symbols()) for term in terms
    ]
    ordered = tuple(
        sorted((tuple(sorted(option)) for option in options), key=lambda x: (len(x), x))
    )
    required = frozenset.intersection(*options)
    return ModuleEvaluation(False, tuple(sorted(required)), ordered)


def referenced_ids(expression: Expression) -> frozenset[str]:
    """Include optional components when reporting which genes relate to a module."""
    if isinstance(expression, Token):
        return frozenset({str(expression)})
    return frozenset(
        str(token)
        for token in expression.scan_values(lambda item: isinstance(item, Token))
    )


def registry_ko_mapping_hash(registry: GeneRegistry) -> str:
    payload = [(record.b_number, list(record.ko_ids)) for record in registry]
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
        entry = KeggModuleEntry(
            module_id, fields["NAME"], fields["DEFINITION"], fields.get("CLASS")
        )
        parse_module_expression(entry.definition)
        entries[module_id] = entry
    if not entries:
        raise ModuleExpressionError("KEGG module flat file contained no entries")
    return entries
