"""Parser and exact completeness evaluator for KEGG Module expressions.

KEGG defines top-level spaces and ``+`` as AND, ``,`` as OR, and ``-`` as an
optional complex component. Whitespace AND is parsed at the lowest precedence
because KEGG requires top-level space-delimited blocks to be parenthesized
before the completeness expression is evaluated.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Literal, TypeAlias

from yggdrisil_ecoli.data.errors import DataValidationError


class ModuleExpressionError(DataValidationError):
    """A KEGG Module expression is malformed or cannot be evaluated exactly."""


@dataclass(frozen=True, slots=True)
class Ko:
    identifier: str


@dataclass(frozen=True, slots=True)
class ModuleRef:
    identifier: str


@dataclass(frozen=True, slots=True)
class OptionalComponent:
    expression: Expression


@dataclass(frozen=True, slots=True)
class And:
    expressions: tuple[Expression, ...]
    operator: Literal["space", "+"]


@dataclass(frozen=True, slots=True)
class Or:
    expressions: tuple[Expression, ...]


Expression: TypeAlias = Ko | ModuleRef | OptionalComponent | And | Or
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

    def as_dict(self) -> dict[str, object]:
        expression = self.expression
        return {
            "module_id": self.module_id,
            "name": self.name,
            "definition": self.definition,
            "module_class": self.module_class,
            "referenced_kos": sorted(referenced_kos(expression)),
            "referenced_modules": sorted(referenced_modules(expression)),
        }


@dataclass(frozen=True, slots=True)
class _Token:
    kind: Literal["ID", "SPACE", "+", "-", ",", "(", ")", "EOF"]
    value: str
    position: int


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


def referenced_kos(expression: Expression) -> frozenset[str]:
    """Return all K numbers directly referenced by an expression."""

    if isinstance(expression, Ko):
        return frozenset({expression.identifier})
    if isinstance(expression, ModuleRef):
        return frozenset()
    if isinstance(expression, OptionalComponent):
        return referenced_kos(expression.expression)
    result: set[str] = set()
    for child in expression.expressions:
        result.update(referenced_kos(child))
    return frozenset(result)


def referenced_modules(expression: Expression) -> frozenset[str]:
    """Return all nested M numbers directly referenced by an expression."""

    if isinstance(expression, ModuleRef):
        return frozenset({expression.identifier})
    if isinstance(expression, Ko):
        return frozenset()
    if isinstance(expression, OptionalComponent):
        return referenced_modules(expression.expression)
    result: set[str] = set()
    for child in expression.expressions:
        result.update(referenced_modules(child))
    return frozenset(result)


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
        if token.kind != kind:
            raise ModuleExpressionError(
                f"expected {kind} at position {token.position}, found {token.value!r}"
            )
        self.index += 1
        return token

    def accept(self, kind: _TokenKind) -> _Token | None:
        if self.current.kind != kind:
            return None
        return self.expect(kind)

    def parse_definition(self) -> Expression:
        expressions = [self.parse_or()]
        while self.accept("SPACE") is not None:
            expressions.append(self.parse_or())
        return _combine_and(expressions, "space")

    def parse_or(self) -> Expression:
        expressions = [self.parse_complex()]
        while self.accept(",") is not None:
            expressions.append(self.parse_complex())
        if len(expressions) == 1:
            return expressions[0]
        flattened: list[Expression] = []
        for expression in expressions:
            if isinstance(expression, Or):
                flattened.extend(expression.expressions)
            else:
                flattened.append(expression)
        return Or(tuple(flattened))

    def parse_complex(self) -> Expression:
        expressions: list[Expression] = [self.parse_primary()]
        saw_operator = False
        while self.current.kind in {"+", "-"}:
            operator = self.current.kind
            self.index += 1
            child = self.parse_primary()
            if operator == "-":
                child = OptionalComponent(child)
            expressions.append(child)
            saw_operator = True
        if not saw_operator:
            return expressions[0]
        return _combine_and(expressions, "+")

    def parse_primary(self) -> Expression:
        token = self.current
        if token.kind == "ID":
            self.index += 1
            if token.value.startswith("K"):
                return Ko(token.value)
            return ModuleRef(token.value)
        if self.accept("(") is not None:
            expression = self.parse_definition()
            self.expect(")")
            return expression
        raise ModuleExpressionError(
            f"expected K number, M number, or '(' at position {token.position}; "
            f"found {token.value!r}"
        )


_TokenKind: TypeAlias = Literal["ID", "SPACE", "+", "-", ",", "(", ")", "EOF"]


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == " ":
            start = index
            while index < len(expression) and expression[index] == " ":
                index += 1
            tokens.append(_Token("SPACE", expression[start:index], start))
            continue
        if char in "+-,()":
            tokens.append(_Token(char, char, index))  # type: ignore[arg-type]
            index += 1
            continue
        if char in {"K", "M"}:
            end = index + 1
            while end < len(expression) and expression[end].isdigit():
                end += 1
            value = expression[index:end]
            if len(value) != 6:
                raise ModuleExpressionError(
                    f"malformed KEGG identifier {value!r} at position {index}"
                )
            tokens.append(_Token("ID", value, index))
            index = end
            continue
        raise ModuleExpressionError(
            f"unexpected character {char!r} at position {index}"
        )
    tokens.append(_Token("EOF", "", len(expression)))
    _reject_spaces_next_to_punctuation(tokens)
    return tokens


def _reject_spaces_next_to_punctuation(tokens: list[_Token]) -> None:
    for index, token in enumerate(tokens):
        if token.kind != "SPACE":
            continue
        previous = tokens[index - 1].kind if index else "EOF"
        following = tokens[index + 1].kind
        if previous in {"+", "-", ",", "("} or following in {"+", "-", ",", ")"}:
            raise ModuleExpressionError(
                f"space next to punctuation at position {token.position} is ambiguous"
            )


def _combine_and(
    expressions: list[Expression], operator: Literal["space", "+"]
) -> Expression:
    if len(expressions) == 1:
        return expressions[0]
    flattened: list[Expression] = []
    for expression in expressions:
        if isinstance(expression, And) and expression.operator == operator:
            flattened.extend(expression.expressions)
        else:
            flattened.append(expression)
    return And(tuple(flattened), operator)


def _completion_options(
    expression: Expression,
    present_kos: frozenset[str],
    module_definitions: dict[str, Expression],
    *,
    stack: tuple[str, ...],
    max_options: int,
) -> frozenset[frozenset[str]]:
    if isinstance(expression, Ko):
        option = (
            frozenset()
            if expression.identifier in present_kos
            else frozenset({expression.identifier})
        )
        return frozenset({option})
    if isinstance(expression, OptionalComponent):
        return frozenset({frozenset()})
    if isinstance(expression, ModuleRef):
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
