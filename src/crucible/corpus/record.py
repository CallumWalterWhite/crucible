"""Recording: run each case against the real warehouse and store its answer (ADR 0013)."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from google.api_core.exceptions import GoogleAPICallError
from google.cloud.bigquery import Dataset, QueryJobConfig, SchemaField

from crucible.corpus.case import Case, Expected, write_recorded
from crucible.corpus.runner import Outcome, compare
from crucible.engine import ErrorKind

if TYPE_CHECKING:
    from google.cloud.bigquery import Client
    from google.cloud.bigquery.table import Row, RowIterator

    from crucible.dialects.base import Dialect

DATASET_PREFIX = "crucible_corpus_"
# Safety net if the process dies before the delete: tables expire, and the label finds leftovers.
DATASET_TABLE_EXPIRATION_MS = 60 * 60 * 1000
DATASET_LABELS = {"crucible-corpus": "1"}

# Legacy and current BigQuery type names -> Standard SQL names (ADR 0013, "Types").
_RENAMED = {
    "INTEGER": "INT64",
    "INT64": "INT64",
    "FLOAT": "FLOAT64",
    "FLOAT64": "FLOAT64",
    "BOOLEAN": "BOOL",
    "BOOL": "BOOL",
}
_AS_IS = frozenset(
    {
        "NUMERIC",
        "BIGNUMERIC",
        "STRING",
        "BYTES",
        "DATE",
        "DATETIME",
        "TIME",
        "TIMESTAMP",
        "JSON",
        "GEOGRAPHY",
        "INTERVAL",
    }
)


class RecordError(Exception):
    """Recording cannot store a trustworthy answer for a case. The run stops."""


class Recorder(Protocol):
    def record(self, case: Case) -> Expected: ...


class BigQueryRecorder:
    """Runs each case in a throwaway dataset with the bytes-billed cap on every job."""

    def __init__(self, client: Client, dialect: Dialect, max_bytes_billed: int) -> None:
        self._client = client
        self._dialect = dialect
        self._max_bytes_billed = max_bytes_billed

    def record(self, case: Case) -> Expected:
        # Dataset API: client.py:703 create_dataset, client.py:1809 delete_dataset.
        dataset = f"{self._client.project}.{DATASET_PREFIX}{uuid.uuid4().hex[:12]}"
        # Dataset: dataset.py, default_table_expiration_ms and labels properties.
        spec = Dataset(dataset)
        spec.default_table_expiration_ms = DATASET_TABLE_EXPIRATION_MS
        spec.labels = DATASET_LABELS
        self._client.create_dataset(spec)
        try:
            # QueryJobConfig: job/query.py:387; default_dataset :489, maximum_bytes_billed :604,
            # use_legacy_sql :698.
            config = QueryJobConfig(
                default_dataset=dataset,
                maximum_bytes_billed=self._max_bytes_billed,
                use_legacy_sql=False,
                # A cached answer is not a fresh query's answer (job/query.py use_query_cache).
                use_query_cache=False,
            )
            for statement in case.setup:
                try:
                    self._client.query(statement, job_config=config).result()
                except GoogleAPICallError as exc:
                    raise RecordError(f"{case.path}: setup failed: {exc.message}") from exc
            try:
                rows = self._client.query(case.query, job_config=config).result()
            except GoogleAPICallError as exc:
                message = _api_message(exc)
                if "bytes billed" in message.lower():
                    # The cap, not the query, stopped it: this is not the warehouse's answer.
                    raise RecordError(
                        f"{case.path}: exceeded maximum_bytes_billed: {message}"
                    ) from exc
                project, dataset_id = dataset.split(".", 1)
                return Expected(error_message=clean_message(message, project, dataset_id))
            return self._rows_expected(rows)
        finally:
            self._client.delete_dataset(dataset, delete_contents=True, not_found_ok=True)

    def _rows_expected(self, rows: RowIterator) -> Expected:
        columns = [(field.name, standard_type(field)) for field in rows.schema]
        types = [kind for _, kind in columns]
        values: list[list[Any]] = []
        # Row and RowIterator are untyped in the client (table.py:1732, :1751); a row's values()
        # is a tuple with one cell per schema column.
        for row in cast("Iterable[Row]", rows):
            cells = cast(tuple[object, ...], row.values())
            values.append(
                [
                    self._dialect.canonical_value(_json_as_text(cell, kind), kind)
                    for cell, kind in zip(cells, types, strict=True)
                ]
            )
        return Expected(columns=columns, rows=values)


def wire_outcomes(cases: list[Case], recorder: BigQueryRecorder) -> list[Outcome]:
    """Run cases over the wire through the recorder's client path (`crucible corpus run --wire`)
    and judge them as the library runner does.

    The wire carries no error kind, so a failed query takes the case's expected kind, except that
    Crucible's own unsupported and internal errors keep theirs and never pass.
    """
    outcomes: list[Outcome] = []
    for case in cases:
        try:
            actual = recorder.record(case)
        except RecordError as exc:
            outcomes.append(Outcome(case, False, str(exc)))
            continue
        message = actual.error_message
        if message is not None:
            if message.startswith("Not supported by Crucible"):
                actual.error_kind = ErrorKind.UNSUPPORTED.value
            elif message.startswith("Crucible internal error"):
                actual.error_kind = ErrorKind.INTERNAL.value
            else:
                actual.error_kind = case.expected.error_kind or ErrorKind.INVALID_QUERY.value
        outcomes.append(compare(case, actual))
    return outcomes


def clean_message(message: str, project: str, dataset: str) -> str:
    """BigQuery's own message for a failed job, as a case file stores it (ADR 0013).

    The client's text repeats the message with `; reason: ..., location: ..., message: ...` and
    appends `Location:` and `Job ID:` lines; only the first message is BigQuery's answer. The
    recording project and throwaway dataset become `<project>` and `<dataset>`, because case files
    are public and the project id is the recorder's, not part of the behaviour.
    """
    primary = message.split("; reason: ", 1)[0].split("\n\nLocation:", 1)[0].strip()
    # Errors raised while polling are prefixed with the request: "GET https://...: <message>".
    primary = re.sub(r"^(GET|POST|PUT|PATCH|DELETE) https?://\S+: ", "", primary)
    for name, placeholder in ((project, "<project>"), (dataset, "<dataset>")):
        # Whole names only: a project id is letters, digits and hyphens; a dataset adds underscores.
        primary = re.sub(rf"(?<![\w-]){re.escape(name)}(?![\w-])", placeholder, primary)
    return primary


def _json_as_text(value: object, warehouse_type: str) -> object:
    """The client parses JSON values; Crucible keeps JSON as text (a JSON null is 'null', not
    NULL). Re-serialising here gives canonical_value the same input from both sides. Nested JSON
    inside ARRAY/STRUCT is not recorded by any case yet and is left as the client returns it."""
    if warehouse_type == "JSON" and value is not None:
        return json.dumps(value)
    return value


def _api_message(exc: GoogleAPICallError) -> str:
    # api_core leaves `message` untyped (exceptions.py:138); it is the text without the HTTP code.
    message = cast(object, getattr(exc, "message", None))
    return message if isinstance(message, str) else str(exc)


def standard_type(field: SchemaField) -> str:
    """Standard SQL name for a BigQuery schema field: RECORD -> STRUCT, REPEATED -> ARRAY."""
    name = field.field_type
    if name == "RECORD":
        # Nested field types: client schema.py:148 SchemaField; mode is uppercased there.
        inner = ", ".join(f"{sub.name} {standard_type(sub)}" for sub in field.fields)
        base = f"STRUCT<{inner}>"
    elif name in _RENAMED:
        base = _RENAMED[name]
    elif name in _AS_IS:
        base = name
    else:
        raise RecordError(f"column {field.name!r}: no Standard SQL name for BigQuery type {name}")
    return f"ARRAY<{base}>" if field.mode == "REPEATED" else base


def record_cases(cases: list[Case], recorder: Recorder, now: Callable[[], datetime]) -> list[Case]:
    """Record each case in order, writing its answer back to its file as it goes.

    Returns the error cases whose message_contains the warehouse's message does not contain.
    Those are written with the verbatim message but stay `verified: false`, for a person to pick
    a new substring; recording never invents one and never stops for it.
    """
    mismatched: list[Case] = []
    for case in cases:
        expected = recorder.record(case)
        verified = True
        if expected.error_message is not None:
            expected.error_kind = case.expected.error_kind
            expected.error_contains = case.expected.error_contains
            wanted = case.expected.error_contains
            if (
                case.expected.error_kind is None
                or wanted is None
                or wanted not in expected.error_message
            ):
                verified = False
                mismatched.append(case)
        write_recorded(case, expected, _iso_utc(now()), verified=verified)
    return mismatched


def _iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("now() must return a timezone-aware datetime")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
