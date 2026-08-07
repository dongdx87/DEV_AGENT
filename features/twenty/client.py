"""HTTP client for a Twenty workspace.

Twenty exposes four surfaces; this client uses the first three:

* ``POST/GET/PATCH/DELETE /rest/<objectNamePlural>`` — record CRUD
* ``POST /graphql``  — record queries (unused for now; REST covers our needs)
* ``POST /metadata`` — GraphQL schema introspection: which objects and fields
  the workspace actually has
* ``POST /mcp``      — the MCP server, for agents rather than for this code

Object and field names are **not** hardcoded. The SAE workspace tracks work in
custom objects (an issue-style object with a key like ``BLS-22``, plus Epics,
Sprints and Merchants), so names are discovered through the metadata API and
mapped in the plugin's settings. Guessing them would break on the first
workspace that is configured differently.

Auth is a workspace API key sent as a bearer token. The key is issued in
Twenty under Settings → API & Webhooks and should be bound to a narrow role
and a dedicated bot workspace member.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Twenty's documented limits: 100 requests/minute and 60 records per request.
#: The client stays under both so a sync never eats the quota a human needs.
DEFAULT_RATE_LIMIT_PER_MINUTE = 60
MAX_RECORDS_PER_REQUEST = 60

DEFAULT_TIMEOUT_SECONDS = 15.0


class TwentyError(Exception):
    """A Twenty call failed. ``message`` is safe to show to an operator."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TwentyRateLimited(TwentyError):
    """Twenty answered 429. The caller should back off rather than retry hard."""


class _TokenBucket:
    """Minimal request-per-minute limiter shared by one client instance."""

    def __init__(self, per_minute: int) -> None:
        self._capacity = max(1, per_minute)
        self._interval = 60.0 / self._capacity
        self._next_at = 0.0
        self._lock = threading.Lock()

    def take(self) -> None:
        """Block just long enough to keep the configured average rate."""
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._interval


@dataclass
class TwentyObject:
    """One object type in the workspace, as reported by the metadata API."""

    name_singular: str
    name_plural: str
    label_singular: str
    is_custom: bool
    fields: dict[str, str] = field(default_factory=dict)

    def field_names(self) -> list[str]:
        return sorted(self.fields)


class TwentyClient:
    """Thin, deterministic client. One instance is scoped to one workspace."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url or not api_key:
            raise TwentyError("Twenty is not configured: base URL and API key are required")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._bucket = _TokenBucket(rate_limit_per_minute)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        # Injected in tests; production builds a client per request so the
        # plugin holds no long-lived sockets.
        self._transport = transport

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        self._bucket.take()
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = client.request(
                    method, url, params=params, json=json_body, headers=self._headers
                )
        except httpx.RequestError as exc:
            raise TwentyError(f"Could not reach Twenty: {exc}") from exc

        if response.status_code == 401:
            raise TwentyError("Twenty rejected the API key (401)", status_code=401)
        if response.status_code == 403:
            raise TwentyError(
                "The API key's role lacks permission for this object (403)",
                status_code=403,
            )
        if response.status_code == 404:
            raise TwentyError(
                f"Twenty resource not found: {path} (404)", status_code=404
            )
        if response.status_code == 429:
            raise TwentyRateLimited(
                "Twenty rate limit hit (429) — slow the sync down", status_code=429
            )
        if response.status_code >= 400:
            raise TwentyError(
                f"Twenty returned an error ({response.status_code})",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise TwentyError("Twenty returned an unreadable response") from exc

    # -- records -----------------------------------------------------------

    def list_records(
        self,
        object_name_plural: str,
        *,
        filter_expression: str | None = None,
        order_by: str | None = None,
        limit: int = MAX_RECORDS_PER_REQUEST,
        depth: int | None = None,
    ) -> list[dict]:
        """Return records of one object type.

        ``filter_expression`` uses Twenty's REST filter syntax, for example
        ``updatedAt[gte]:"2026-08-01T00:00:00Z"``. It is passed through
        untouched so the caller owns the query and this client stays generic.
        """
        params: dict[str, Any] = {"limit": min(limit, MAX_RECORDS_PER_REQUEST)}
        if filter_expression:
            params["filter"] = filter_expression
        if order_by:
            params["orderBy"] = order_by
        if depth is not None:
            params["depth"] = depth

        payload = self._request("GET", f"/rest/{object_name_plural}", params=params)
        return _extract_records(payload, object_name_plural)

    def get_record(
        self, object_name_plural: str, record_id: str, *, depth: int | None = None
    ) -> dict:
        params = {"depth": depth} if depth is not None else None
        payload = self._request(
            "GET", f"/rest/{object_name_plural}/{record_id}", params=params
        )
        records = _extract_records(payload, object_name_plural)
        if not records:
            raise TwentyError(
                f"Record {record_id} was not found in {object_name_plural}",
                status_code=404,
            )
        return records[0]

    def update_record(
        self, object_name_plural: str, record_id: str, changes: dict
    ) -> dict:
        """PATCH one record. Only send fields that actually changed."""
        payload = self._request(
            "PATCH", f"/rest/{object_name_plural}/{record_id}", json_body=changes
        )
        records = _extract_records(payload, object_name_plural)
        return records[0] if records else {}

    def create_record(self, object_name_plural: str, values: dict) -> dict:
        payload = self._request(
            "POST", f"/rest/{object_name_plural}", json_body=values
        )
        records = _extract_records(payload, object_name_plural)
        return records[0] if records else {}

    # -- metadata ----------------------------------------------------------

    #: Enough of the schema to let an operator map objects/fields in settings.
    _OBJECTS_QUERY = """
    query PluginObjectMetadata {
      objects(paging: { first: 200 }) {
        edges {
          node {
            nameSingular
            namePlural
            labelSingular
            isCustom
            isActive
            fields(paging: { first: 200 }) {
              edges { node { name type isActive } }
            }
          }
        }
      }
    }
    """

    def describe_objects(self, *, include_inactive: bool = False) -> list[TwentyObject]:
        """List the workspace's objects and their fields.

        This is what makes the plugin work against a customised workspace: the
        SAE board is built on custom objects, so the operator picks the task
        object and maps its fields instead of this code assuming names.
        """
        payload = self._request(
            "POST", "/metadata", json_body={"query": self._OBJECTS_QUERY}
        )
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            first = errors[0].get("message") if isinstance(errors[0], dict) else errors[0]
            raise TwentyError(f"Metadata query failed: {first}")

        edges = (
            (payload.get("data") or {}).get("objects", {}).get("edges", [])
            if isinstance(payload, dict)
            else []
        )
        objects: list[TwentyObject] = []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            if not include_inactive and node.get("isActive") is False:
                continue
            fields = {}
            for field_edge in ((node.get("fields") or {}).get("edges") or []):
                field_node = (field_edge or {}).get("node") or {}
                name = field_node.get("name")
                if not name:
                    continue
                if not include_inactive and field_node.get("isActive") is False:
                    continue
                fields[name] = field_node.get("type") or "UNKNOWN"
            objects.append(
                TwentyObject(
                    name_singular=node.get("nameSingular") or "",
                    name_plural=node.get("namePlural") or "",
                    label_singular=node.get("labelSingular") or "",
                    is_custom=bool(node.get("isCustom")),
                    fields=fields,
                )
            )
        return objects

    # -- links -------------------------------------------------------------

    def record_url(self, object_name_singular: str, record_id: str) -> str:
        """Human-facing URL of a record, using Twenty's show-page path."""
        return f"{self._base_url}/object/{object_name_singular}/{record_id}"

    def ping(self) -> bool:
        """Cheap authenticated call, used by the preflight page."""
        self._request("POST", "/metadata", json_body={"query": "{ __typename }"})
        return True


def _extract_records(payload: Any, object_name_plural: str) -> list[dict]:
    """Normalise the several shapes Twenty's REST layer returns.

    A list call answers ``{"data": {"<plural>": [...]}}``, a single-record call
    ``{"data": {"<singular>": {...}}}``, and some deployments answer with the
    record or list directly. Accepting all of them keeps the client working
    across versions instead of failing on a wrapper change.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []

    keyed = data.get(object_name_plural)
    if isinstance(keyed, list):
        return [item for item in keyed if isinstance(item, dict)]

    # Single-record responses are keyed by the singular name, which we do not
    # know here — take the only dict/list value present.
    values = [value for value in data.values() if isinstance(value, (dict, list))]
    if len(values) == 1:
        only = values[0]
        if isinstance(only, list):
            return [item for item in only if isinstance(item, dict)]
        return [only]

    if "id" in data:
        return [data]
    return []
