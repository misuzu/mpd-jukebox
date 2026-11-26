"""myMPD's local subset of MPD filter expressions (§1.9).

Grammar, as in myMPD's `lib/search/search.c`:

* clauses are joined with `) AND (` only, for example
  `((Artist == 'A') AND (Genre != 'B'))`;
* a clause is `tag op 'value'`, with `op` one of `==`, `!=`, `contains`,
  `starts_with`, `=~`, `!~`;
* special clauses: `any op 'v'`, `file == 'uri'`, `base 'dir'`,
  `modified-since 'date'`, `added-since 'date'`, `prio >= N` and
  `AudioFormat op 'v'` (accepted and ignored).

Differences from myMPD: fuzzy `~~` is reported as `Unsupported` (a config
error), `file` takes MPD's `==` operator, and dates are read as UTC.
"""

import calendar
import enum
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .model import Tags
from .tags import MPD_TAGS, canonical_tag, normalize

_ANY_TAGS = tuple(sorted(MPD_TAGS))


class Kind(enum.Enum):
    TAG = 'tag'
    ANY = 'any'
    FILE = 'file'
    BASE = 'base'
    MODIFIED_SINCE = 'modified-since'
    ADDED_SINCE = 'added-since'
    PRIO = 'prio'
    AUDIO_FORMAT = 'audioformat'


class Op(enum.Enum):
    EQUAL = '=='
    NOT_EQUAL = '!='
    CONTAINS = 'contains'
    STARTS_WITH = 'starts_with'
    REGEX = '=~'
    NOT_REGEX = '!~'
    GREATER_EQUAL = '>='
    FUZZY = '~~'


_NEGATIVE = frozenset({Op.NOT_EQUAL, Op.NOT_REGEX})
_TAG_OPS = {op.value: op for op in Op if op is not Op.GREATER_EQUAL}
_SPECIAL_KINDS = {
    'any': Kind.ANY,
    'file': Kind.FILE,
    'base': Kind.BASE,
    'modified-since': Kind.MODIFIED_SINCE,
    'added-since': Kind.ADDED_SINCE,
    'prio': Kind.PRIO,
    'audioformat': Kind.AUDIO_FORMAT,
}
# Kinds whose operator is implied, like in MPD: `(base 'dir')`.
_NO_OP_KINDS = frozenset({Kind.BASE, Kind.MODIFIED_SINCE, Kind.ADDED_SINCE})


@dataclass(frozen=True, slots=True)
class Clause:
    kind: Kind
    op: Op
    value: str
    tag: str | None = None
    folded: str = ''
    number: int = 0
    regex: re.Pattern[str] | None = None


@dataclass(frozen=True, slots=True)
class Filter:
    clauses: tuple[Clause, ...]


@dataclass(frozen=True, slots=True)
class Invalid:
    reason: str


@dataclass(frozen=True, slots=True)
class Unsupported:
    reason: str


ParseResult = Filter | Invalid | Unsupported


@dataclass(frozen=True, slots=True)
class Subject:
    """What a filter is evaluated against. Albums have no `uri`/`prio`."""

    tags: Tags
    uri: str | None = None
    prio: int | None = None
    last_modified: int = 0
    added: int = 0


class _ParseError(Exception):
    pass


class _UnsupportedError(Exception):
    pass


def parse(expression: str) -> ParseResult:
    try:
        clauses = tuple(
            _parse_clause(token.strip('() '))
            for token in expression.split(') AND (')
        )
    except _ParseError as e:
        return Invalid(str(e))
    except _UnsupportedError as e:
        return Unsupported(str(e))
    return Filter(clauses)


def _take_word(text: str) -> tuple[str, str]:
    """Split at the first space; the rest has leading spaces removed."""
    word, sep, rest = text.partition(' ')
    if not sep:
        raise _ParseError(f'unexpected end of expression: {text!r}')
    return word, rest.lstrip(' ')


def _parse_clause(text: str) -> Clause:
    tag_name, rest = _take_word(text)
    tag = canonical_tag(tag_name)
    if tag is not None:
        kind = Kind.TAG
    elif tag_name.lower() in _SPECIAL_KINDS:
        kind = _SPECIAL_KINDS[tag_name.lower()]
    else:
        raise _ParseError(f'unknown tag {tag_name!r}')

    if kind in _NO_OP_KINDS:
        op = Op.STARTS_WITH if kind is Kind.BASE else Op.GREATER_EQUAL
    else:
        op_name, rest = _take_word(rest)
        op = _parse_op(kind, op_name)

    if kind is Kind.PRIO:
        if not rest.isdigit():
            raise _ParseError(f'prio needs a number: {rest!r}')
        return Clause(kind, op, rest, number=int(rest))

    value = _unquote(rest)
    if kind in (Kind.MODIFIED_SINCE, Kind.ADDED_SINCE):
        return Clause(kind, op, value, number=_parse_date(value))
    regex = None
    if op in (Op.REGEX, Op.NOT_REGEX):
        try:
            regex = re.compile(value.casefold())
        except re.error as e:
            raise _ParseError(f'invalid regex {value!r}: {e}') from e
    return Clause(kind, op, value, tag, normalize(value), regex=regex)


def _parse_op(kind: Kind, name: str) -> Op:
    if kind is Kind.PRIO:
        if name != '>=':
            raise _ParseError(f'prio needs >=, not {name!r}')
        return Op.GREATER_EQUAL
    if kind is Kind.FILE:
        if name != '==':
            raise _ParseError(f'file needs ==, not {name!r}')
        return Op.EQUAL
    op = _TAG_OPS.get(name)
    if op is None:
        raise _ParseError(f'unknown operator {name!r}')
    if op is Op.FUZZY:
        raise _UnsupportedError('fuzzy matching (~~) is not supported')
    return op


def _unquote(text: str) -> str:
    if len(text) < 2 or text[0] not in '\'"' or text[-1] != text[0]:
        raise _ParseError(f'value must be quoted: {text!r}')
    body = text[1:-1]
    out = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == '\\':
            if i + 1 >= len(body):
                raise _ParseError(f'dangling escape in {text!r}')
            i += 1
            c = body[i]
        out.append(c)
        i += 1
    return ''.join(out)


def _parse_date(value: str) -> int:
    if value.isdigit():
        return int(value)
    try:
        parsed = time.strptime(value[:10], '%Y-%m-%d')
    except ValueError as e:
        raise _ParseError(f'invalid date {value!r}') from e
    return calendar.timegm(parsed)


def _match(value: str, clause: Clause) -> bool:
    match clause.op:
        case Op.CONTAINS:
            return clause.folded in value
        case Op.STARTS_WITH:
            return value.startswith(clause.folded)
        case Op.EQUAL:
            return value == clause.folded
        case Op.NOT_EQUAL:
            return value != clause.folded
        case Op.REGEX:
            assert clause.regex is not None
            return clause.regex.search(value) is not None
        case Op.NOT_REGEX:
            assert clause.regex is not None
            return clause.regex.search(value) is None
    raise AssertionError(clause.op)  # pragma: no cover


def _done(rc: bool, negative: bool) -> bool:
    """myMPD's exit_search_loop: positive operators stop at the first match,
    negative ones at the first mismatch."""
    return rc != negative


def _match_tags(tags: Tags, names: tuple[str, ...], clause: Clause) -> bool:
    negative = clause.op in _NEGATIVE
    rc = False
    for name in names:
        found = tags.get(name, ())
        rc = False
        for value in found:
            rc = _match(normalize(value), clause)
            if _done(rc, negative):
                break
        if not found:
            rc = negative
        if _done(rc, negative):
            break
    return rc


def _clause_matches(clause: Clause, subject: Subject) -> bool:
    match clause.kind:
        case Kind.MODIFIED_SINCE:
            return clause.number <= subject.last_modified
        case Kind.ADDED_SINCE:
            return clause.number <= subject.added
        case Kind.FILE:
            return subject.uri is None or subject.uri == clause.value
        case Kind.BASE:
            return subject.uri is None or subject.uri.startswith(clause.value)
        case Kind.PRIO:
            return subject.prio is None or clause.number <= subject.prio
        case Kind.AUDIO_FORMAT:
            return True
        case Kind.ANY:
            return _match_tags(subject.tags, _ANY_TAGS, clause)
        case Kind.TAG:
            assert clause.tag is not None
            return _match_tags(subject.tags, (clause.tag,), clause)
    raise AssertionError(clause.kind)  # pragma: no cover


def evaluate(flt: Filter, subject: Subject) -> bool:
    return all(_clause_matches(c, subject) for c in flt.clauses)


def predicate(
    include: Filter | None, exclude: Filter | None
) -> Callable[[Subject], bool]:
    """myMPD's check_expression_*: exclude wins over include; no include
    means include everything."""

    def check(subject: Subject) -> bool:
        if exclude is not None and evaluate(exclude, subject):
            return False
        return include is None or evaluate(include, subject)

    return check


def tags_used(flt: Filter) -> frozenset[str] | None:
    """The tags a filter reads; None if it can read any tag (`any`)."""
    tags = set()
    for clause in flt.clauses:
        if clause.kind is Kind.ANY:
            return None
        if clause.tag is not None:
            tags.add(clause.tag)
    return frozenset(tags)


def usable(result: ParseResult) -> Filter | None:
    """The filter to apply: myMPD ignores invalid expressions."""
    return result if isinstance(result, Filter) else None
