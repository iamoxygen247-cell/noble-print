"""
fake_graph.py — an in-memory stand-in for Microsoft Graph.

This is a fake ``requests.Session``, not a mock of our own functions, so the REAL
adapter and transport code runs against it: URL construction, the retry loop,
paging, eTag handling, error parsing and the upload protocol are all exercised.
It is the analogue of the FakeTable in the sibling invoice project.

Two deliberate choices make it catch real bugs:

* Column internal names default to the ENCODED form ("Print_x005f_Status"), so
  every test that touches a field is also a test that no display name was
  hardcoded into a Graph call.

* Every request is recorded WITH ITS HEADERS. Two calls in this pipeline must not
  carry an Authorization header (SharePoint's pre-authenticated download URL and
  the Universal Print upload PUT, which Graph documents may 401 if you add one),
  and headers-in-the-log is what lets a test assert that.
"""

from __future__ import annotations

import json as jsonlib
import pathlib
import re
import urllib.parse
from typing import Any, Dict, List, Optional

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Dict[str, Any]:
    """Read a captured real-world response. utf-8-sig because Windows tooling
    writes JSON with a BOM and json.loads rejects it."""
    return jsonlib.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))

GRAPH = "https://graph.microsoft.com/v1.0"
DOWNLOAD_HOST = "https://noble.sharepoint.example/download"
UPLOAD_HOST = "https://print.print.microsoft.example/uploadSessions"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None,
                 headers: Optional[Dict[str, str]] = None, body: bytes = b""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._body = body

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    @property
    def content(self) -> bytes:
        if self._body:
            return self._body
        return b"" if self._payload is None else jsonlib.dumps(self._payload).encode()

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class Call:
    def __init__(self, method: str, url: str, headers: Dict[str, str], body: Any):
        self.method = method
        self.url = url
        self.headers = headers or {}
        self.body = body

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.url).path

    def __repr__(self) -> str:
        return "<{} {}>".format(self.method, self.url)


def _encode(name: str) -> str:
    """SharePoint's internal-name encoding for an underscore."""
    return name.replace("_", "_x005f_")


class FakeGraph:
    """A tenant with one site, one library and one printer share."""

    SITE_ID = "contoso.sharepoint.com,site-guid,web-guid"
    LIST_ID = "list-guid"
    SHARE_ID = "share-guid"
    PRINTER_ID = "printer-guid"

    def __init__(self, encoded_columns: bool = True):
        transform = _encode if encoded_columns else (lambda n: n)
        self.columns = {
            "Print_Status": transform("Print_Status"),
            "Print_JobId": transform("Print_JobId"),
            "Print_Message": transform("Print_Message"),
            "Printer_Name": transform("Printer_Name"),
        }
        self.list_title = "Documents"
        self.items: Dict[str, Dict[str, Any]] = {}
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Call] = []
        self.cancelled: List[str] = []
        self.page_size = 100
        self.drop_columns: List[str] = []
        self._next_job = 1800
        self._clock = 0

        self.share = {
            "id": self.SHARE_ID,
            "displayName": "Front Office MFC",
            "isAcceptingJobs": True,
            "capabilities": {"contentTypes": ["application/pdf", "application/oxps"]},
            "status": {"state": "idle"},
            "printer": {"id": self.PRINTER_ID},
        }
        # A tenant can have several shares, each backed by a different printer.
        # That matters because cancel is addressed by PRINTER id, so a job must
        # be cancelled on the printer it was actually submitted to.
        self.shares = {self.SHARE_ID: self.share}

        self._failures: List[Dict[str, Any]] = []

    # -- fixture building ------------------------------------------------------

    def add_item(self, item_id: str, *, status: str = "PRINT_READY",
                 created: str = "2026-08-01T10:00:00Z", name: str = "invoice.pdf",
                 folder: str = "/sites/Ops/Shared Documents/Invoices/ToPrint",
                 job_id: str = "", printer: str = "", message: str = "",
                 size: int = 1024, modified: Optional[str] = None) -> Dict[str, Any]:
        fields = {
            "FileLeafRef": name,
            "FileDirRef": folder,
            self.columns["Print_Status"]: status,
            self.columns["Print_JobId"]: job_id,
            self.columns["Print_Message"]: message,
            self.columns["Printer_Name"]: printer,
        }
        self.items[item_id] = {
            "id": item_id,
            "eTag": '"{},1"'.format(item_id),
            "createdDateTime": created,
            "lastModifiedDateTime": modified or created,
            "name": name,
            "fields": fields,
            "size": size,
            "version": 1,
        }
        return self.items[item_id]

    def use_real_printer(self) -> Dict[str, Any]:
        """Replace the invented share with Noble's actual Universal Print
        registration (tests/fixtures/printer_brother_dcp_l2540dw.json).

        The point is not realism for its own sake: the real printer id and share
        id are two different GUIDs, so a test that mixes them up fails with a
        legible diff rather than with two lookalike placeholders. That is the
        defect F3 shape.
        """
        data = load_fixture("printer_brother_dcp_l2540dw.json")
        share = data["share"]

        self.SHARE_ID = share["id"]
        self.PRINTER_ID = share["printer"]["id"]
        self.share = {
            "id": share["id"],
            "displayName": share["displayName"],
            "isAcceptingJobs": share["isAcceptingJobs"],
            "capabilities": {
                "contentTypes": list(share["capabilities"]["contentTypes"]),
            },
            "status": dict(share["status"]),
            "printer": {"id": share["printer"]["id"]},
        }
        self.shares = {share["id"]: self.share}
        return data

    def add_share(self, share_id: str, *, printer_id: str,
                  display_name: str = "Second printer",
                  content_types: Optional[List[str]] = None) -> Dict[str, Any]:
        share = {
            "id": share_id,
            "displayName": display_name,
            "isAcceptingJobs": True,
            "capabilities": {"contentTypes": content_types
                             or ["application/pdf", "application/oxps"]},
            "status": {"state": "idle"},
            "printer": {"id": printer_id},
        }
        self.shares[share_id] = share
        return share

    def add_job(self, job_id: str, state: str = "pending",
                description: str = "", details: Optional[List[str]] = None,
                acknowledged: Optional[str] = None) -> None:
        """A print job.

        `acknowledged` is printJob.acknowledgedDateTime -- "the dateTimeOffset
        when the job was acknowledged". The fake did not model it at first, which
        is precisely why the design spent months asserting the job "carries no
        timestamp": the only shape anyone tested against was this one. It is
        modelled now, and defaults to absent so the fallback path stays covered.
        """
        self.jobs[job_id] = {
            "id": job_id,
            "isFetchable": False,
            "errorCode": 0,
            "status": {
                "state": state,
                "description": description or state,
                "details": details or [],
                "isAcquiredByPrinter": False,
            },
        }
        if acknowledged:
            self.jobs[job_id]["acknowledgedDateTime"] = acknowledged

    def fail_next(self, method: str, url_contains: str, *, status: int = 500,
                  payload: Any = None, times: int = 1) -> None:
        """Make the next `times` matching calls fail, so each failure branch of
        the submission runs through the real error handling."""
        self._failures.append({
            "method": method.upper(),
            "match": url_contains,
            "times": times,
            "response": FakeResponse(status, payload or {
                "error": {"code": "injected",
                          "message": "injected {}".format(status)}}),
        })

    # -- assertions helpers ----------------------------------------------------

    def field(self, item_id: str, display_name: str) -> str:
        return self.items[item_id]["fields"].get(self.columns[display_name], "")

    def status_of(self, item_id: str) -> str:
        return self.field(item_id, "Print_Status")

    def created_jobs(self) -> List[Call]:
        """Only the create-job POSTs.

        calls_to("/jobs") is a trap: createUploadSession and /start both carry
        "/jobs/" in their URLs, so a naive count reports three calls per single
        submission.
        """
        return [c for c in self.calls
                if c.method == "POST" and c.url.split("?")[0].endswith("/jobs")]

    def calls_to(self, fragment: str, method: Optional[str] = None) -> List[Call]:
        return [c for c in self.calls
                if fragment in c.url and (method is None or c.method == method.upper())]

    # -- the requests.Session interface ---------------------------------------

    def request(self, method: str, url: str, json=None, headers=None,
                timeout=None) -> FakeResponse:
        method = method.upper()
        self.calls.append(Call(method, url, dict(headers or {}), json))

        for failure in self._failures:
            if (failure["times"] > 0 and failure["method"] == method
                    and failure["match"] in url):
                failure["times"] -= 1
                return failure["response"]

        split = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(split.query)
        prefix = urllib.parse.urlsplit(GRAPH).path
        rel = split.path[len(prefix):] if split.path.startswith(prefix) else split.path

        handler = self._route(method, rel)
        if handler is None:
            return FakeResponse(404, {"error": {
                "code": "unknownRoute",
                "message": "no fake route for {} {}".format(method, rel)}})
        return handler(rel, query, json, headers or {})

    # -- routing ---------------------------------------------------------------

    _ROUTES = [
        ("GET", r"^/sites/[^/]+$", "_get_site"),
        # /sites/{hostname}:/sites/{name} -- the colon form has slashes after it
        ("GET", r"^/sites/[^/]+:(/[^/]*)*$", "_get_site"),
        ("GET", r"^/sites/[^/]+/lists/[^/]+$", "_get_list"),
        ("GET", r"^/sites/[^/]+/lists/[^/]+/items$", "_get_items"),
        ("GET", r"^/sites/[^/]+/lists/[^/]+/items/[^/]+/driveItem$", "_get_drive_item"),
        ("PATCH", r"^/sites/[^/]+/lists/[^/]+/items/[^/]+/fields$", "_patch_fields"),
        ("GET", r"^/print/shares/[^/]+$", "_get_share"),
        ("POST", r"^/print/shares/[^/]+/jobs$", "_create_job"),
        ("POST", r".*/createUploadSession$", "_create_upload_session"),
        ("POST", r"^/print/shares/[^/]+/jobs/[^/]+/start$", "_start_job"),
        ("GET", r"^/print/shares/[^/]+/jobs/[^/]+$", "_get_job"),
        ("POST", r"^/print/printers/[^/]+/jobs/[^/]+/cancel$", "_cancel_job"),
    ]

    def _route(self, method: str, rel: str):
        for verb, pattern, name in self._ROUTES:
            if method == verb and re.match(pattern, rel):
                return getattr(self, name)
        return None

    # -- SharePoint ------------------------------------------------------------

    def _get_site(self, rel, query, body, headers):
        return FakeResponse(200, {"id": self.SITE_ID})

    def _get_list(self, rel, query, body, headers):
        columns = [{"name": internal, "displayName": display}
                   for display, internal in self.columns.items()
                   if display not in self.drop_columns]
        columns.append({"name": "FileLeafRef", "displayName": "Name"})
        return FakeResponse(200, {
            "id": self.LIST_ID,
            "name": self.list_title,
            "displayName": self.list_title,
            "columns": columns,
        })

    def _get_items(self, rel, query, body, headers):
        raw_filter = (query.get("$filter") or [""])[0]
        match = re.search(r"fields/(\S+)\s+eq\s+'([^']*)'", raw_filter)
        rows = list(self.items.values())
        if match:
            column, wanted = match.group(1), match.group(2)
            rows = [r for r in rows if r["fields"].get(column) == wanted]

        skip = int((query.get("$skip") or ["0"])[0])
        page = rows[skip:skip + self.page_size]
        payload: Dict[str, Any] = {
            "value": [{"id": r["id"], "eTag": r["eTag"],
                       "createdDateTime": r["createdDateTime"],
                       "lastModifiedDateTime": r["lastModifiedDateTime"],
                       "name": r["name"], "fields": dict(r["fields"])}
                      for r in page]
        }
        if skip + self.page_size < len(rows):
            # Carry the whole query forward, as Graph does. Without this the
            # filter vanishes after page 1 and the fake silently returns rows the
            # real service never would.
            forward = {k: v[:] for k, v in query.items()}
            forward["$skip"] = [str(skip + self.page_size)]
            payload["@odata.nextLink"] = "{}{}?{}".format(
                GRAPH, rel, urllib.parse.urlencode(forward, doseq=True))
        return FakeResponse(200, payload)

    def _get_drive_item(self, rel, query, body, headers):
        item_id = rel.split("/items/")[1].split("/")[0]
        item = self.items.get(item_id)
        if item is None:
            return FakeResponse(404, {"error": {"code": "itemNotFound",
                                                "message": "no such item"}})
        return FakeResponse(200, {
            "id": "drive-" + item_id,
            "name": item["name"],
            "size": item["size"],
            "file": {"mimeType": "application/pdf"},
            "@microsoft.graph.downloadUrl": "{}/{}?token=abc".format(
                DOWNLOAD_HOST, item_id),
        })

    def _patch_fields(self, rel, query, body, headers):
        item_id = rel.split("/items/")[1].split("/")[0]
        item = self.items.get(item_id)
        if item is None:
            return FakeResponse(404, {"error": {"code": "itemNotFound",
                                                "message": "no such item"}})
        if_match = headers.get("If-Match")
        if if_match and if_match != item["eTag"]:
            return FakeResponse(412, {"error": {"code": "preconditionFailed",
                                                "message": "eTag mismatch"}})
        item["fields"].update(body or {})
        item["version"] += 1
        item["eTag"] = '"{},{}"'.format(item_id, item["version"])
        # SharePoint bumps the modified stamp on every write. Resubmit orders on
        # it, so the fake must model it or the starvation test is meaningless.
        self._clock += 1
        item["lastModifiedDateTime"] = "2026-08-30T{:02d}:{:02d}:00Z".format(
            12 + self._clock // 60, self._clock % 60)
        return FakeResponse(200, dict(item["fields"]))

    # -- Universal Print -------------------------------------------------------

    def _get_share(self, rel, query, body, headers):
        share_id = rel.split("/print/shares/")[1]
        share = self.shares.get(share_id)
        if share is None:
            return FakeResponse(404, {"error": {"code": "shareNotFound",
                                                "message": "no such share"}})
        return FakeResponse(200, dict(share))

    def _create_job(self, rel, query, body, headers):
        self._next_job += 1
        job_id = str(self._next_job)
        self.add_job(job_id, state="paused", description="uploadPending",
                     details=["uploadPending"])
        return FakeResponse(201, {
            "id": job_id,
            "status": self.jobs[job_id]["status"],
            "documents": [{"id": "doc-" + job_id, "displayName": "", "size": 0}],
        })

    def _create_upload_session(self, rel, query, body, headers):
        job_id = rel.split("/jobs/")[1].split("/")[0]
        return FakeResponse(200, {
            "uploadUrl": "{}/{}?tempauthtoken=xyz".format(UPLOAD_HOST, job_id),
            "expirationDateTime": "2026-12-31T00:00:00Z",
        })

    def _start_job(self, rel, query, body, headers):
        job_id = rel.split("/jobs/")[1].split("/")[0]
        if job_id in self.jobs:
            self.jobs[job_id]["status"] = {
                "state": "processing",
                "description": "The print job is currently being processed.",
                "details": ["interpreting"],
                "isAcquiredByPrinter": False,
            }
        return FakeResponse(200, self.jobs.get(job_id, {}).get("status", {}))

    def _get_job(self, rel, query, body, headers):
        job_id = rel.split("/jobs/")[1]
        job = self.jobs.get(job_id)
        if job is None:
            return FakeResponse(404, {"error": {"code": "jobNotFound",
                                                "message": "no such job"}})
        return FakeResponse(200, dict(job))

    def _cancel_job(self, rel, query, body, headers):
        printer_id = rel.split("/print/printers/")[1].split("/")[0]
        job_id = rel.split("/jobs/")[1].split("/")[0]
        known_printers = {s["printer"]["id"] for s in self.shares.values()}
        if printer_id not in known_printers:
            return FakeResponse(404, {"error": {"code": "printerNotFound",
                                                "message": "no such printer"}})
        self.cancelled.append(job_id)
        if job_id in self.jobs:
            self.jobs[job_id]["status"] = {
                "state": "canceled", "description": "Canceled.",
                "details": [], "isAcquiredByPrinter": False,
            }
        return FakeResponse(204)


class FakeAnonSession:
    """The unauthenticated session: SharePoint downloads and print uploads.

    Records headers so a test can assert no Authorization header is ever sent,
    and implements the upload protocol's 202-then-201 contract so a truncated
    upload is detectable.
    """

    def __init__(self, content: bytes = b"%PDF-1.7 fake document"):
        self.content = content
        self.calls: List[Call] = []
        self.uploaded: Dict[str, bytearray] = {}
        self.deleted: List[str] = []
        self.download_status = 200
        self.put_status_override: Optional[int] = None

    def get(self, url, timeout=None):
        self.calls.append(Call("GET", url, {}, None))
        if self.download_status != 200:
            return FakeResponse(self.download_status,
                                {"error": {"code": "gone", "message": "expired"}})
        return FakeResponse(200, body=self.content)

    def put(self, url, data=None, headers=None, timeout=None):
        headers = dict(headers or {})
        self.calls.append(Call("PUT", url, headers, data))
        self.uploaded.setdefault(url, bytearray()).extend(data or b"")

        if self.put_status_override is not None:
            return FakeResponse(self.put_status_override, {})

        match = re.match(r"bytes (\d+)-(\d+)/(\d+)", headers.get("Content-Range", ""))
        if match and int(match.group(2)) == int(match.group(3)) - 1:
            return FakeResponse(201, {"id": "doc", "size": int(match.group(3))})
        return FakeResponse(202, {"nextExpectedRanges": ["0-"]})

    def delete(self, url, timeout=None):
        self.deleted.append(url)
        self.calls.append(Call("DELETE", url, {}, None))
        return FakeResponse(204)
