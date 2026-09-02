"""
graph_client.py — HTTP transport for Microsoft Graph.

Knows about tokens, retries, throttling and paging. Knows NOTHING about
SharePoint or printing: both adapters sit on top of this and neither concept
appears below. See docs/design.md §5.2.

Three things here are load-bearing and easy to get wrong:

1. UNAUTHENTICATED REQUESTS ARE A FIRST-CLASS PATH, not an oversight. Two calls
   in this pipeline must NOT carry an Authorization header:
     * the SharePoint pre-authenticated download URL
       ("You don't need to include an Authorization header when you access the
        download URL" -- driveitem-get-content)
     * the Universal Print upload session PUT
       ("Including the Authorization header when making the PUT call might result
        in an HTTP 401 Unauthorized" -- upload-data-to-upload-session)
   Both URLs carry their own short-lived token in the query string. A generic
   "authenticated client" would break both, silently in the first case and with a
   confusing 401 in the second, so they get their own session with no auth on it
   and a test asserts the header is absent.

2. NOT EVERYTHING MAY BE RETRIED. Creating a print job is not idempotent: a retry
   after a response we failed to read produces a second job and a second printed
   page. Callers opt out with retry=False, and the default backoff never applies
   to a 4xx other than 429.

3. THE PRINT SERVICE'S CORRELATION HEADERS ARE THE ONLY THING SUPPORT CAN USE.
   `request-id` and `X-MSEdge-Ref` are captured onto every error so they reach
   the log line instead of being discarded with the response object.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Callable, Dict, Iterator, Optional, Sequence

import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

DEFAULT_TIMEOUT_SECONDS = 30.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 300.0
MAX_ATTEMPTS = 3
RETRYABLE_STATUS = (429, 503, 504)
# A Retry-After longer than this is not worth waiting for inside a request that
# has its own wall-clock budget; fail fast and let the next scheduled run try.
MAX_RETRY_AFTER_SECONDS = 20.0
# Stop runaway paging. At a batch of 5 this is far more than any real query needs;
# it exists so a filter bug cannot walk an entire library.
DEFAULT_MAX_PAGES = 20


def resolve_timeout_seconds() -> float:
    """The per-call HTTP timeout: GRAPH_TIMEOUT_SECONDS, else the constant.

    Resolved here rather than in print_policy because a socket timeout is
    transport configuration, not a business rule -- this module must keep
    importing no domain (docs/design.md §5.2). It follows the same contract as
    every other tunable: an out-of-range ENV value warns and falls back, because
    a server misconfiguration must not fail every call (§6.5).

    Read per call, not captured at import, so a settings change takes effect on
    the next invocation rather than the next cold start.
    """
    raw = os.getenv("GRAPH_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logging.warning("GRAPH_TIMEOUT_SECONDS=%s is not a number; using %s",
                        raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
        logging.warning("GRAPH_TIMEOUT_SECONDS=%s is outside [%s, %s]; using %s",
                        raw, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS,
                        DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    return value


class GraphError(Exception):
    """A Graph call that failed. Carries the pieces a human needs to act:
    the status, the service's own error text, and the correlation ids."""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 request_id: Optional[str] = None, edge_ref: Optional[str] = None,
                 url: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.edge_ref = edge_ref
        self.url = url

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status_code is not None:
            parts.append(f"(HTTP {self.status_code})")
        if self.request_id:
            parts.append(f"request-id={self.request_id}")
        return " ".join(parts)


def _correlation(response: requests.Response) -> Dict[str, Optional[str]]:
    return {
        "request_id": response.headers.get("request-id")
                      or response.headers.get("client-request-id"),
        "edge_ref": response.headers.get("X-MSEdge-Ref"),
    }


def _error_text(response: requests.Response) -> str:
    """Graph's structured error message if there is one, else the raw body.
    The structured form is far more useful in Print_Message than a JSON blob."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:500]
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or ""
        code = error.get("code") or ""
        return f"{code}: {message}".strip(": ") if (code or message) else str(payload)[:500]
    return str(payload)[:500]


def _sleep_for(response: Optional[requests.Response], attempt: int) -> float:
    """Retry-After if the service gave one, otherwise exponential backoff with
    jitter. Jitter matters even at this volume: without it, a batch that trips a
    throttle retries in lockstep and trips it again."""
    if response is not None:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return min(float(raw), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
    return min(2.0 ** attempt, 8.0) + random.uniform(0, 0.5)


class GraphClient:
    """Authenticated Graph transport.

    `token_provider` is a zero-argument callable returning a bearer token; it is
    called per request so a token refreshed mid-invocation is picked up without
    rebuilding the client.
    """

    def __init__(self, token_provider: Callable[[], str],
                 timeout: Optional[float] = None,
                 session: Optional[requests.Session] = None):
        self._token_provider = token_provider
        # None means "whatever the environment says"; an explicit value is an
        # override, so a caller with a reason can still pin it.
        self._timeout = timeout if timeout is not None else resolve_timeout_seconds()
        self._session = session or requests.Session()

    # -- core ------------------------------------------------------------------

    def request(self, method: str, url: str, *, json_body: Any = None,
                headers: Optional[Dict[str, str]] = None,
                expected: Sequence[int] = (200,),
                retry: bool = True,
                allow_status: Sequence[int] = ()) -> requests.Response:
        """One Graph call, with throttling handled.

        `expected`     statuses treated as success.
        `allow_status` statuses returned to the caller instead of raising -- used
                       for 404 (a job that aged out) and 412 (a lost claim), both
                       of which are ordinary control flow here, not errors.
        `retry=False`  for non-idempotent calls; see the module docstring.
        """
        if not url.startswith("http"):
            url = f"{GRAPH_BASE}{url}"

        last_error: Optional[GraphError] = None
        for attempt in range(MAX_ATTEMPTS):
            request_headers = {
                "Authorization": f"Bearer {self._token_provider()}",
                "Accept": "application/json",
            }
            if json_body is not None:
                request_headers["Content-Type"] = "application/json"
            if headers:
                request_headers.update(headers)

            try:
                response = self._session.request(
                    method, url, json=json_body, headers=request_headers,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                last_error = GraphError(f"{method} {url} failed: {exc}", url=url)
                if not retry or attempt == MAX_ATTEMPTS - 1:
                    raise last_error
                time.sleep(_sleep_for(None, attempt))
                continue

            if response.status_code in expected or response.status_code in allow_status:
                return response

            correlation = _correlation(response)
            last_error = GraphError(
                f"{method} {url} -> {_error_text(response)}",
                status_code=response.status_code, url=url, **correlation,
            )

            retryable = retry and response.status_code in RETRYABLE_STATUS
            if not retryable or attempt == MAX_ATTEMPTS - 1:
                raise last_error

            delay = _sleep_for(response, attempt)
            logging.warning("Graph %s %s -> %s; retrying in %.1fs (attempt %s/%s)",
                            method, url, response.status_code, delay,
                            attempt + 2, MAX_ATTEMPTS)
            time.sleep(delay)

        raise last_error or GraphError(f"{method} {url} failed", url=url)

    # -- convenience -----------------------------------------------------------

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None,
                 allow_status: Sequence[int] = ()) -> Optional[dict]:
        response = self.request("GET", url, headers=headers, allow_status=allow_status)
        if response.status_code in allow_status and response.status_code not in (200,):
            return None
        return response.json()

    def post_json(self, url: str, body: Any = None,
                  headers: Optional[Dict[str, str]] = None,
                  expected: Sequence[int] = (200, 201),
                  retry: bool = True) -> Optional[dict]:
        response = self.request("POST", url, json_body=body, headers=headers,
                                expected=expected, retry=retry)
        if response.status_code == 204 or not (response.content or b"").strip():
            return None
        return response.json()

    def patch_json(self, url: str, body: Any,
                   headers: Optional[Dict[str, str]] = None,
                   allow_status: Sequence[int] = ()) -> Optional[requests.Response]:
        return self.request("PATCH", url, json_body=body, headers=headers,
                            expected=(200, 204), allow_status=allow_status)

    def paged(self, url: str, headers: Optional[Dict[str, str]] = None,
              max_pages: int = DEFAULT_MAX_PAGES) -> Iterator[dict]:
        """Yield items across @odata.nextLink pages, up to a hard page cap.

        The cap is a safety net, not a paging strategy: hitting it means the
        filter is wrong, and a warning says so rather than quietly truncating.
        """
        next_url: Optional[str] = url
        for page in range(max_pages):
            payload = self.get_json(next_url, headers=headers) or {}
            for item in payload.get("value", []):
                yield item
            next_url = payload.get("@odata.nextLink")
            if not next_url:
                return
        logging.warning("paging stopped at the %s-page cap for %s; "
                        "the filter is probably too broad", max_pages, url)


# --- Unauthenticated transport ------------------------------------------------
# Separate session, no Authorization header, ever. See the module docstring.

_anon_session = requests.Session()


def download_unauthenticated(url: str, timeout: Optional[float] = None) -> bytes:
    """GET a pre-authenticated URL (SharePoint @microsoft.graph.downloadUrl).

    These URLs expire within minutes, so they are fetched immediately after being
    read and never cached.

    RETRIED, UNLIKE THE PRINT-JOB CALLS. A GET of a document is idempotent --
    exception 2 in the module docstring is about creating a job, where a retry
    prints a second page, and nothing here can print anything. What it protects
    against is a reset mid-handshake: measured on a development workstation
    running TLS interception, two of three identical attempts died with
    WinError 10054 and the third returned the file. Without a retry that is a
    claimed row landing at PRINT_FAILED for a reason outside the pipeline, and a
    human resetting the column to try again.

    The backoff stays well inside the URL's lifetime: three attempts, capped at
    8s plus jitter each.
    """
    timeout = timeout if timeout is not None else resolve_timeout_seconds()

    last_error: Optional[GraphError] = None
    for attempt in range(MAX_ATTEMPTS):
        response = None
        try:
            response = _anon_session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last_error = GraphError(f"download failed: {exc}", url=url)
        else:
            if response.status_code == 200:
                return response.content
            last_error = GraphError(f"download failed: {_error_text(response)}",
                                    status_code=response.status_code, url=url,
                                    **_correlation(response))
            if response.status_code not in RETRYABLE_STATUS:
                raise last_error

        if attempt == MAX_ATTEMPTS - 1:
            raise last_error

        delay = _sleep_for(response, attempt)
        logging.warning("download failed (%s); retrying in %.1fs (attempt %s/%s)",
                        last_error, delay, attempt + 2, MAX_ATTEMPTS)
        time.sleep(delay)

    raise last_error or GraphError("download failed", url=url)


def put_unauthenticated(url: str, data: bytes, headers: Dict[str, str],
                        expected: Sequence[int] = (200, 201, 202),
                        timeout: Optional[float] = None) -> requests.Response:
    """PUT one byte range to a Universal Print upload session URL.

    No Authorization header: the upload URL carries its own tempauthtoken and
    Graph documents that adding a bearer token may produce a 401.
    """
    timeout = timeout if timeout is not None else resolve_timeout_seconds()
    try:
        response = _anon_session.put(url, data=data, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise GraphError(f"upload failed: {exc}", url=url)
    if response.status_code not in expected:
        raise GraphError(f"upload failed: {_error_text(response)}",
                         status_code=response.status_code, url=url, **_correlation(response))
    return response


def delete_unauthenticated(url: str, timeout: Optional[float] = None) -> None:
    """Cancel an abandoned upload session. Best-effort by contract: the caller is
    already handling a failure and must not be derailed by the cleanup."""
    timeout = timeout if timeout is not None else resolve_timeout_seconds()
    try:
        _anon_session.delete(url, timeout=timeout)
    except requests.RequestException:
        logging.warning("upload session cleanup failed for %s", url, exc_info=True)
