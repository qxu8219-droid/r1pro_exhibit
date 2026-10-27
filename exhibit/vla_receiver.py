"""Read-only, resumable robot event receiver. Independent of camera reception."""
from http.client import HTTPException
import json
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from evidence import _check_finite, _reject_constant
from launcher import NoRedirect

MAX_BATCH_BYTES = 8 * 1024 * 1024


class ReceiverError(ValueError):
    pass


class Receiver:
    def __init__(self, source_url, store, interval=0.25, timeout=2):
        parsed = urlsplit(source_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("VLA source must be an HTTP(S) origin without credentials or query")
        if not .01 <= interval <= 10 or not .1 <= timeout <= 10:
            raise ValueError("Invalid receiver interval or timeout")
        self.url = source_url.rstrip("/")
        self.store, self.interval, self.timeout = store, interval, timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self._source_id, self._cursor = None, 0
        self._stop = threading.Event()
        self.last_error = None
        self.thread = threading.Thread(target=self._run, name="vla-receiver", daemon=True)

    def poll_once(self):
        try:
            with self.opener.open(self.url + "/events?after=%d&limit=200" % self._cursor,
                                  timeout=self.timeout) as response:
                length = response.headers.get("Content-Length")
                raw = response.read(MAX_BATCH_BYTES + 1)
            if len(raw) > MAX_BATCH_BYTES:
                raise ReceiverError("VLA event batch is too large")
            if length is not None and int(length) != len(raw):
                raise ReceiverError("Incomplete VLA event batch")
            batch = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
            _check_finite(batch)
            if not isinstance(batch, dict):
                raise ReceiverError("Invalid VLA batch")
            source_id = batch.get("source_id")
            cursor, events, more = batch.get("next_cursor"), batch.get("events"), batch.get("has_more")
            health = batch.get("health")
            if (not isinstance(source_id, str) or not source_id or len(source_id) > 256
                    or type(cursor) is not int or cursor < 0
                    or not isinstance(events, list) or len(events) > 200
                    or type(more) is not bool or not isinstance(health, dict)):
                raise ReceiverError("Invalid VLA batch envelope")
            if source_id != self._source_id:
                previous_cursor = self._cursor
                saved = self.store.cursor(source_id)
                self._source_id, self._cursor = source_id, saved
                # Fetch at the correct offset before accepting a different source.
                if saved != previous_cursor:
                    return True
            if cursor < self._cursor or ((events or more) and cursor == self._cursor):
                raise ReceiverError("VLA cursor did not advance")
            self.store.ingest(events, source_id, cursor, health=health)
            self._cursor = cursor
            self.last_error = None
            return more
        except HTTPError as exc:
            if exc.code in (409, 416):
                self._source_id, self._cursor = None, 0
                return True
            raise ReceiverError("VLA source HTTP %d" % exc.code) from exc
        except ReceiverError:
            raise
        except (OSError, HTTPException, ValueError, TypeError, RecursionError, OverflowError) as exc:
            # Do not include response bodies or endpoint secrets in diagnostics.
            raise ReceiverError("VLA event reception failed (%s)" % type(exc).__name__) from exc

    def _run(self):
        while not self._stop.is_set():
            try:
                more = self.poll_once()
            except Exception as exc:
                self.last_error = type(exc).__name__ + ": " + str(exc)
                more = False
            # Even a long backlog yields, so shutdown remains responsive.
            self._stop.wait(.01 if more else self.interval)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self._stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=self.timeout + 2)
            if self.thread.is_alive():
                raise RuntimeError("VLA receiver did not finish its read")
