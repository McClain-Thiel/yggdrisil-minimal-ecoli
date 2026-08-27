"""Parser and exact completeness evaluator for KEGG Module expressions.

KEGG defines top-level spaces and ``+`` as AND, ``,`` as OR, and ``-`` as an
optional complex component. Whitespace AND is parsed at the lowest precedence
because KEGG requires top-level space-delimited blocks to be parenthesized
before the completeness expression is evaluated.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Literal, TypeAlias

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry


class ModuleExpressionError(DataValidationError):
    """A KEGG Module expression is malformed or cannot be evaluated exactly."""


@dataclass(frozen=True, slots=True)
class Reference:
    identifier: str


@dataclass(frozen=True, slots=True)
class OptionalComponent:
    expression: Expression


@dataclass(frozen=True, slots=True)
class And:
    expressions: tuple[Expression, ...]


@dataclass(frozen=True, slots=True)
class Or:
    expressions: tuple[Expression, ...]


Expression: TypeAlias = Reference | OptionalComponent | And | Or
PARSER_SEMANTICS_VERSION = "1"


@dataclass(frozen=True, slots=True)
class ModuleEvaluation:
    complete: bool
    missing_required_kos: tuple[str, ...]
    minimal_missing_ko_sets: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class KeggModuleEntry:
    """Fields required to reconstruct one frozen module definition."""

    module_id: str
    name: str
    definition: str
    module_class: str | None

    @property
    def expression(self) -> Expression:
        return parse_module_expression(self.definition)


_Token: TypeAlias = tuple[str, str, int]


def parse_module_expression(raw: str) -> Expression:
    """Parse one canonical KEGG Module logical expression."""

    expression = raw.strip()
    if not expression:
        raise ModuleExpressionError("module expression is empty")
    parser = _Parser(_tokenize(expression))
    result = parser.parse_definition()
    parser.expect("EOF")
    return result


def evaluate_module_expression(
    expression: Expression,
    present_kos: set[str] | frozenset[str],
    *,
    module_definitions: dict[str, Expression] | None = None,
    max_options: int = 4096,
) -> ModuleEvaluation:
    """Evaluate completeness and exact subset-minimal KO additions.

    The evaluator raises instead of approximating if the alternative set grows
    beyond ``max_options``. This keeps an expensive expression visible rather
    than returning a biologically misleading partial answer.
    """

    if max_options < 1:
        raise ValueError("max_options must be positive")
    options = _completion_options(
        expression,
        frozenset(present_kos),
        module_definitions or {},
        stack=(),
        max_options=max_options,
    )
    if frozenset() in options:
        return ModuleEvaluation(
            complete=True,
            missing_required_kos=(),
            minimal_missing_ko_sets=(),
        )
    ordered_options = tuple(
        sorted((tuple(sorted(option)) for option in options), key=lambda x: (len(x), x))
    )
    required = set(ordered_options[0]) if ordered_options else set()
    for option in ordered_options[1:]:
        required.intersection_update(option)
    return ModuleEvaluation(
        complete=False,
        missing_required_kos=tuple(sorted(required)),
        minimal_missing_ko_sets=ordered_options,
    )


def referenced_ids(expression: Expression) -> frozenset[str]:
    """Return all K and M identifiers directly referenced by an expression."""

    if isinstance(expression, Reference):
        return frozenset({expression.identifier})
    if isinstance(expression, OptionalComponent):
        return referenced_ids(expression.expression)
    result: set[str] = set()
    for child in expression.expressions:
        result.update(referenced_ids(child))
    return frozenset(result)


def registry_ko_mapping_hash(registry: GeneRegistry) -> str:
    """Fingerprint the canonical gene-to-KO mapping used by module evaluation."""

    payload = [(record.b_number, list(record.ko_ids)) for record in registry]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode()
    ).hexdigest()


def parse_kegg_module_flat_file(path: str | Path) -> dict[str, KeggModuleEntry]:
    """Parse KEGG DBGET flat-file records returned by ``get``."""

    with Path(path).open(encoding="utf-8") as handle:
        return parse_kegg_module_flat_text(handle.read())


def parse_kegg_module_flat_text(raw: str) -> dict[str, KeggModuleEntry]:
    """Parse one or more ``///``-delimited KEGG Module records."""

    entries: dict[str, KeggModuleEntry] = {}
    for record_number, record in enumerate(raw.split("///"), start=1):
        if not record.strip():
            continue
        fields: dict[str, list[str]] = {}
        current_field: str | None = None
        for line in record.splitlines():
            if not line.strip():
                continue
            label = line[:12].strip()
            value = line[12:].strip()
            if label:
                current_field = label
                fields.setdefault(label, []).append(value)
            elif current_field is not None:
                fields[current_field].append(value)
            else:
                raise ModuleExpressionError(
                    f"record {record_number}: continuation without a field"
                )
        entry_tokens = " ".join(fields.get("ENTRY", ())).split()
        if not entry_tokens or not _is_module_id(entry_tokens[0]):
            raise ModuleExpressionError(
                f"record {record_number}: missing or malformed ENTRY"
            )
        module_id = entry_tokens[0]
        if module_id in entries:
            raise ModuleExpressionError(f"duplicate module entry: {module_id}")
        name = " ".join(fields.get("NAME", ())).strip()
        definition = " ".join(fields.get("DEFINITION", ())).strip()
        if not name or not definition:
            raise ModuleExpressionError(
                f"{module_id}: missing NAME or DEFINITION field"
            )
        # Parse now so malformed snapshots fail at the source boundary.
        parse_module_expression(definition)
        module_class = " ".join(fields.get("CLASS", ())).strip() or None
        entries[module_id] = KeggModuleEntry(
            module_id=module_id,
            name=name,
            definition=definition,
            module_class=module_class,
        )
    if not entries:
        raise ModuleExpressionError("KEGG module flat file contained no entries")
    return entries


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def expect(self, kind: _TokenKind) -> _Token:
        token = self.current
        if token[0] != kind:
            raise ModuleExpressionError(
                f"expected {kind} at position {token[2]}, found {token[1]!r}"
            )
        self.index += 1
        return token

    def accept(self, kind: _TokenKind) -> bool:
        if self.current[0] != kind:
            return False
        self.index += 1
        return True

    def parse_definition(self) -> Expression:
        expressions = [self.parse_or()]
        while self.accept("SPACE"):
            expressions.append(self.parse_or())
        return _combine(expressions, And)

    def parse_or(self) -> Expression:
        expressions = [self.parse_complex()]
        while self.accept(","):
            expressions.append(self.parse_complex())
        return _combine(expressions, Or)

    def parse_complex(self) -> Expression:
        expressions: list[Expression] = [self.parse_primary()]
        saw_operator = False
        while self.current[0] in {"+", "-"}:
            optional = self.current[0] == "-"
            self.index += 1
            child = self.parse_primary()
            if optional:
                child = OptionalComponent(child)
            expressions.append(child)
            saw_operator = True
        if not saw_operator:
            return expressions[0]
        return _combine(expressions, And)

    def parse_primary(self) -> Expression:
        token = self.current
        if token[0] == "ID":
            self.index += 1
            return Reference(token[1])
        if self.accept("("):
            expression = self.parse_definition()
            self.expect(")")
            return expression
        raise ModuleExpressionError(
            f"expected K number, M number, or '(' at position {token[2]}; "
            f"found {token[1]!r}"
        )


_TokenKind: TypeAlias = Literal["ID", "SPACE", "+", "-", ",", "(", ")", "EOF"]


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    for match in re.finditer(r"[KM][0-9]{5}| +|[+\-,()]", expression):
        if match.start() != position:
            raise ModuleExpressionError(
                f"unexpected character {expression[position]!r} at position {position}"
            )
        value = match.group()
        kind = "ID" if value[0] in "KM" else "SPACE" if value[0] == " " else value
        tokens.append((kind, value, position))
        position = match.end()
    if position != len(expression):
        raise ModuleExpressionError(
            f"unexpected character {expression[position]!r} at position {position}"
        )
    tokens.append(("EOF", "", len(expression)))
    _reject_spaces_next_to_punctuation(tokens)
    return tokens


def _reject_spaces_next_to_punctuation(tokens: list[_Token]) -> None:
    for index, token in enumerate(tokens):
        if token[0] != "SPACE":
            continue
        previous = tokens[index - 1][0] if index else "EOF"
        following = tokens[index + 1][0]
        if previous in {"+", "-", ",", "("} or following in {"+", "-", ",", ")"}:
            raise ModuleExpressionError(
                f"space next to punctuation at position {token[2]} is ambiguous"
            )


def _combine(expressions: list[Expression], kind: type[And] | type[Or]) -> Expression:
    if len(expressions) == 1:
        return expressions[0]
    flattened: list[Expression] = []
    for expression in expressions:
        if isinstance(expression, kind):
            flattened.extend(expression.expressions)
        else:
            flattened.append(expression)
    return kind(tuple(flattened))


def _completion_options(
    expression: Expression,
    present_kos: frozenset[str],
    module_definitions: dict[str, Expression],
    *,
    stack: tuple[str, ...],
    max_options: int,
) -> frozenset[frozenset[str]]:
    if isinstance(expression, Reference):
        if expression.identifier.startswith("K"):
            missing: frozenset[str] = frozenset()
            if expression.identifier not in present_kos:
                missing = frozenset({expression.identifier})
            return frozenset({missing})
        if expression.identifier in stack:
            chain = " -> ".join((*stack, expression.identifier))
            raise ModuleExpressionError(f"cyclic module reference: {chain}")
        try:
            target = module_definitions[expression.identifier]
        except KeyError as exc:
            raise ModuleExpressionError(
                f"unresolved module reference: {expression.identifier}"
            ) from exc
        return _completion_options(
            target,
            present_kos,
            module_definitions,
            stack=(*stack, expression.identifier),
            max_options=max_options,
        )
    if isinstance(expression, OptionalComponent):
        return frozenset({frozenset()})
    child_options = [
        _completion_options(
            child,
            present_kos,
            module_definitions,
            stack=stack,
            max_options=max_options,
        )
        for child in expression.expressions
    ]
    if isinstance(expression, Or):
        return _minimal_options(
            (option for options in child_options for option in options), max_options
        )
    combined = (
        frozenset().union(*option_tuple) for option_tuple in product(*child_options)
    )
    return _minimal_options(combined, max_options)


def _minimal_options(
    candidates: Iterable[frozenset[str]], max_options: int
) -> frozenset[frozenset[str]]:
    minimal: set[frozenset[str]] = set()
    for candidate in candidates:
        if any(existing <= candidate for existing in minimal):
            continue
        minimal = {existing for existing in minimal if not candidate < existing}
        minimal.add(candidate)
        if len(minimal) > max_options:
            raise ModuleExpressionError(
                f"module expression exceeds exact option limit ({max_options})"
            )
    return frozenset(minimal)


def _is_module_id(value: str) -> bool:
    return len(value) == 6 and value.startswith("M") and value[1:].isdigit()
