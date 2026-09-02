"""
sharepoint.py — SharePoint adapter: find the library, read the queue, write the
four columns, fetch the bytes.

Knows Graph's site/list/driveItem URL shapes. Knows NOTHING about what a print
status means -- every status string arrives as an argument. That is what lets a
different workflow reuse this file unchanged (docs/design.md §9).

Three decisions here are worth knowing before changing anything:

COLUMN NAMES ARE RESOLVED, NEVER HARDCODED. SharePoint fixes a column's internal
name at creation and encodes characters the display name cannot carry directly
(documented: _x0020_ for a space, _x003a_ for a colon; underscores are widely
reported to become _x005f_, so "Print_Status" may really be "Print_x005f_Status").
Guessing is a coin flip, so the list's own column definitions are read once and
cached, and everything downstream uses the resolved name.

ORDERING AND WINDOWING HAPPEN IN PYTHON. $orderby on fields/* is not documented
as supported for list items and is widely reported to fail, and Graph will only
filter one indexed field at a time. So the query filters on status alone and
print_policy does the rest. This is why Print_Status must be INDEXED in
SharePoint: a non-indexed column cannot be used in $filter at all.

THE CLAIM IS AN ETAG-CONDITIONED PATCH. `if-match` is documented on
PATCH .../items/{id}/fields: on a mismatch Graph returns 412 "and the item will
not be updated". That is the whole double-print guard -- of two overlapping runs,
exactly one wins each file.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from typing import Any, Dict, List, Optional

import graph_client
import print_policy
from graph_client import GraphClient, GraphError

# Ask SharePoint to honour a filter on a column that may not be indexed. The
# column SHOULD be indexed (see the module docstring); this header means a
# missing index degrades to "may fail under load" instead of "always fails".
PREFER_NON_INDEXED = {"Prefer": "HonorNonIndexedQueriesWarningMayFailRandomly"}

# SharePoint system fields present on document libraries.
FIELD_DIR = "FileDirRef"    # server-relative folder, e.g. /sites/X/Shared Documents/Invoices
FIELD_LEAF = "FileLeafRef"  # file name with extension


class ColumnNotFound(RuntimeError):
    """A required column could not be resolved by display name.

    Deliberately fatal. Continuing without it would run the whole pipeline and
    then fail to record any outcome -- files would print and their status would
    never change, so they would print again on the next run.
    """


@dataclass
class ListContext:
    """Everything needed to talk to one library, resolved once per request."""
    site_id: str
    list_id: str
    list_title: str
    columns: Dict[str, str]  # display name -> internal name

    def internal(self, display_name: str) -> str:
        try:
            return self.columns[display_name]
        except KeyError:
            raise ColumnNotFound(
                f"column {display_name!r} was not found in library "
                f"{self.list_title!r}. Available columns: "
                f"{', '.join(sorted(self.columns)) or '(none)'}"
            )

    @property
    def items_url(self) -> str:
        return f"/sites/{self.site_id}/lists/{self.list_id}/items"


@dataclass
class PrintFile:
    """One queue row, flattened to what the orchestrator actually uses."""
    item_id: str
    etag: Optional[str]
    created: Optional[datetime]
    # When SharePoint last wrote the row. OUR OWN patches bump it, which makes it
    # a "last attempted" stamp. Poll uses it as the start of the current attempt
    # when there is no print job to read a createdDateTime from -- a crashed
    # submission -- which is how the retry schedule places such a row without an
    # attempt counter (see print_policy.poll_decision).
    modified: Optional[datetime]
    file_name: str
    folder: str
    status: str
    job_id: str
    printer: str
    message: str
    fields: Dict[str, Any] = dataclass_field(default_factory=dict)

    @property
    def id(self) -> str:  # for print_policy.select_oldest tie-breaking
        return self.item_id


# --- resolution ---------------------------------------------------------------


def resolve_site(client: GraphClient, hostname: str, site_path: str) -> str:
    """Site id from a hostname and server-relative path.

    Graph's colon syntax: /sites/{hostname}:/sites/{name}. A site path of "" or
    "/" addresses the root site.
    """
    path = (site_path or "").strip()
    if path and not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/")
    url = f"/sites/{hostname}:{path}" if path else f"/sites/{hostname}"
    payload = client.get_json(url) or {}
    site_id = payload.get("id")
    if not site_id:
        raise GraphError(f"could not resolve site {hostname}{path}")
    return site_id


def resolve_list(client: GraphClient, site_id: str, library: str) -> ListContext:
    """Resolve a library and its column definitions in ONE call.

    `library` may be a list id or a list title -- Graph accepts both at
    /sites/{site}/lists/{id-or-title} -- so callers can pass the friendly name
    they see in SharePoint.
    """
    quoted = urllib.parse.quote(library, safe="")
    url = (f"/sites/{site_id}/lists/{quoted}"
           f"?$select=id,name,displayName&$expand=columns($select=name,displayName)")
    payload = client.get_json(url) or {}

    list_id = payload.get("id")
    if not list_id:
        raise GraphError(f"could not resolve library {library!r} on site {site_id}")

    columns: Dict[str, str] = {}
    for column in payload.get("columns", []):
        display = column.get("displayName")
        internal = column.get("name")
        if display and internal:
            columns[display] = internal

    context = ListContext(
        site_id=site_id, list_id=list_id,
        list_title=payload.get("displayName") or payload.get("name") or library,
        columns=columns,
    )

    # Fail now, loudly, naming the column -- not later, after printing.
    missing = [name for name in print_policy.COLUMN_DISPLAY_NAMES if name not in columns]
    if missing:
        raise ColumnNotFound(
            f"library {context.list_title!r} is missing required column(s): "
            f"{', '.join(missing)}. Expected display names: "
            f"{', '.join(print_policy.COLUMN_DISPLAY_NAMES)}."
        )
    logging.info("resolved library %r: %s", context.list_title,
                 ", ".join(f"{d}->{i}" for d, i in sorted(columns.items())
                           if d in print_policy.COLUMN_DISPLAY_NAMES))
    return context


# --- reading ------------------------------------------------------------------


def _to_print_file(context: ListContext, item: Dict[str, Any]) -> PrintFile:
    fields = item.get("fields") or {}

    # Graph can omit FileDirRef from expanded list-item fields. Fall back to
    # the list item's webUrl and remove the filename to obtain its folder.
    web_path = urllib.parse.unquote(
        urllib.parse.urlsplit(str(item.get("webUrl") or "")).path
    )
    web_folder = web_path.rpartition("/")[0] if web_path else ""
    folder = fields.get(FIELD_DIR) or web_folder

    return PrintFile(
        item_id=str(item.get("id", "")),
        etag=item.get("eTag"),
        created=print_policy.parse_graph_datetime(item.get("createdDateTime")),
        modified=print_policy.parse_graph_datetime(item.get("lastModifiedDateTime")),
        file_name=fields.get(FIELD_LEAF) or item.get("name") or "",
        folder=folder,
        status=(fields.get(context.internal(print_policy.COLUMN_STATUS)) or "").strip(),
        job_id=(fields.get(context.internal(print_policy.COLUMN_JOB_ID)) or "").strip(),
        printer=(fields.get(context.internal(print_policy.COLUMN_PRINTER)) or "").strip(),
        message=(fields.get(context.internal(print_policy.COLUMN_MESSAGE)) or "").strip(),
        fields=fields,
    )


def query_by_status(client: GraphClient, context: ListContext, status: str,
                    folder: str = "") -> List[PrintFile]:
    """Every item in the library with `status`, scoped to `folder` in Python.

    `$expand=fields` without a select is deliberate: the system fields used for
    folder scoping (FileDirRef, FileLeafRef) are not part of the resolved column
    map, and enumerating them in a select is one more thing to get wrong for no
    saving at these volumes.
    """
    status_column = context.internal(print_policy.COLUMN_STATUS)
    query = urllib.parse.urlencode({
        "$expand": "fields",
        "$filter": f"fields/{status_column} eq '{status}'",
    })
    url = f"{context.items_url}?{query}"

    files = [_to_print_file(context, item)
             for item in client.paged(url, headers=PREFER_NON_INDEXED)]
    if not folder:
        return files
    return [f for f in files if print_policy.folder_matches(f.folder, folder)]


def get_download_url(client: GraphClient, context: ListContext,
                     item_id: str) -> Dict[str, Any]:
    """The driveItem behind a list item, with its pre-authenticated download URL.

    listItem exposes a documented `driveItem` relationship for document
    libraries. The download URL expires within minutes, so it is fetched here
    immediately before use and never cached.
    """
    url = (f"{context.items_url}/{item_id}/driveItem"
           f"?$select=id,name,size,file,@microsoft.graph.downloadUrl")
    payload = client.get_json(url) or {}
    download_url = payload.get("@microsoft.graph.downloadUrl")
    if not download_url:
        raise GraphError(f"list item {item_id} has no downloadable driveItem "
                         f"(is it a folder?)")
    return {
        "download_url": download_url,
        "name": payload.get("name") or "",
        "size": int(payload.get("size") or 0),
        "content_type": ((payload.get("file") or {}).get("mimeType") or ""),
    }


def download(download_url: str, timeout: Optional[float] = None) -> bytes:
    """Fetch the file bytes. No Authorization header -- the URL is
    pre-authenticated and Graph documents that none is needed."""
    return graph_client.download_unauthenticated(download_url, timeout=timeout)


# --- writing ------------------------------------------------------------------


def patch_fields(client: GraphClient, context: ListContext, item_id: str,
                 values: Dict[str, Any], etag: Optional[str] = None) -> bool:
    """Write column values. Returns False if an `etag` was supplied and lost.

    Keys are DISPLAY names; they are translated to internal names here so no
    caller ever has to think about SharePoint's encoding.

    A False return is ordinary control flow, not an error: it means a concurrent
    invocation claimed this file first and this one must leave it alone.
    """
    body = {context.internal(display): value for display, value in values.items()}
    headers = {"If-Match": etag} if etag else None

    response = client.patch_json(
        f"{context.items_url}/{item_id}/fields", body,
        headers=headers, allow_status=(412,),
    )
    if response is not None and response.status_code == 412:
        logging.info("claim lost on item %s (412); another run is handling it", item_id)
        return False
    return True
