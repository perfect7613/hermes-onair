"""obs-websocket v5 client.

The whole interface is ``call`` / ``call_batch`` / ``subscribe`` / ``hold`` / ``close``. Behind it:
lazy connect, authentication, request/response correlation, a reader thread, event dispatch off
the reader thread, one transparent retry when the socket died before a request left, and a
reconnect loop while the connection is *held* (a live session wants events even when idle).

Nothing connects until the first ``call``. The transport is a seam: production uses the
``websockets`` library, tests pass an in-memory fake.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from .errors import ObsAuthError, ObsRequestError, ObsTimeout, ObsUnavailable

logger = logging.getLogger(__name__)

RPC_VERSION = 1
# Every event category except the high-volume ones (volume meters, transform spam).
EVENT_SUBSCRIPTIONS = 2047

OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED = 0, 1, 2
OP_EVENT, OP_REQUEST, OP_RESPONSE, OP_BATCH, OP_BATCH_RESPONSE = 5, 6, 7, 8, 9

CLOSE_AUTH_FAILED = 4009
MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # screenshots arrive as base64 inside one frame

EventCallback = Callable[[dict[str, Any]], None]


class TransportClosed(Exception):
    """The socket is gone. ``code`` is the WebSocket close code when the peer sent one."""

    def __init__(self, reason: str = "", code: Optional[int] = None) -> None:
        super().__init__(reason or "connection closed")
        self.code = code


class Transport(Protocol):
    def send(self, text: str) -> None: ...

    def recv(self, timeout: Optional[float] = None) -> str:
        """Next text frame. Raises ``TimeoutError`` or ``TransportClosed``."""

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str = "127.0.0.1"
    port: int = 4455
    password: str = ""

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"


Connector = Callable[[Endpoint, float], Transport]


def auth_response(password: str, salt: str, challenge: str) -> str:
    """The obs-websocket challenge response: b64(sha256(b64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def websocket_connector(endpoint: Endpoint, timeout: float) -> Transport:
    """Production transport over the ``websockets`` sync client (ships with Hermes)."""
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    class _WsTransport:
        def __init__(self) -> None:
            self._ws = connect(endpoint.url, open_timeout=timeout, max_size=MAX_MESSAGE_BYTES,
                               subprotocols=["obswebsocket.json"])  # type: ignore[list-item]

        def send(self, text: str) -> None:
            try:
                self._ws.send(text)
            except ConnectionClosed as exc:
                raise TransportClosed(str(exc), _close_code(exc)) from exc
            except OSError as exc:
                raise TransportClosed(str(exc)) from exc

        def recv(self, timeout: Optional[float] = None) -> str:
            try:
                frame = self._ws.recv(timeout=timeout)
            except ConnectionClosed as exc:
                raise TransportClosed(str(exc), _close_code(exc)) from exc
            except OSError as exc:
                raise TransportClosed(str(exc)) from exc
            return frame if isinstance(frame, str) else frame.decode()

        def close(self) -> None:
            try:
                self._ws.close()
            except Exception:  # closing a dead socket must never raise
                pass

    try:
        return _WsTransport()
    except (OSError, TimeoutError) as exc:
        raise ObsUnavailable(f"Could not reach OBS at {endpoint.url}") from exc
    except Exception as exc:  # handshake rejected: something else is listening on that port
        raise ObsUnavailable(f"{endpoint.url} did not answer as OBS ({exc})") from exc


def _close_code(exc: Exception) -> Optional[int]:
    received = getattr(exc, "rcvd", None)
    return getattr(received, "code", None)


class _Pending:
    """One in-flight request: the caller waits on ``done``; the reader fills ``payload`` or ``error``."""

    __slots__ = ("done", "payload", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.payload: Optional[dict[str, Any]] = None
        self.error: Optional[Exception] = None


class _Connection:
    """One identified socket and its reader thread. Replaced wholesale on reconnect."""

    def __init__(self, transport: Transport, version: dict[str, Any]) -> None:
        self.transport = transport
        self.version = version
        self.alive = True
        self.send_lock = threading.Lock()


class ObsClient:
    def __init__(self, endpoint: Callable[[], Endpoint], connector: Connector = websocket_connector,
                 *, connect_timeout: float = 4.0) -> None:
        self._endpoint = endpoint  # re-read on every connect so a changed password applies at once
        self._connector = connector
        self._connect_timeout = connect_timeout
        self._conn: Optional[_Connection] = None
        self._conn_lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._listeners: dict[str, list[EventCallback]] = {}
        self._listeners_lock = threading.Lock()
        self._events: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue()
        self._dispatcher: Optional[threading.Thread] = None
        self._held = False
        self._closed = False
        self._wake = threading.Event()
        self._reconnector: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ public interface

    @property
    def connected(self) -> bool:
        conn = self._conn
        return bool(conn and conn.alive)

    def call(self, request_type: str, data: Optional[dict[str, Any]] = None, *,
             timeout: float = 10.0) -> dict[str, Any]:
        """Send one request and return its ``responseData`` (``{}`` when OBS sends none)."""
        request = {"requestType": request_type, "requestData": data or {}}
        payload = self._roundtrip(OP_REQUEST, request, timeout, label=request_type)
        return _unwrap(request_type, payload)

    def call_batch(self, requests: list[tuple[str, dict[str, Any]]], *, halt_on_failure: bool = False,
                   timeout: float = 15.0) -> list[dict[str, Any] | ObsRequestError]:
        """Run requests in order inside OBS. Each slot is the response data or the refusal."""
        if not requests:
            return []
        body = {
            "haltOnFailure": halt_on_failure,
            "requests": [{"requestType": name, "requestData": data or {}} for name, data in requests],
        }
        payload = self._roundtrip(OP_BATCH, body, timeout, label="batch")
        results: list[dict[str, Any] | ObsRequestError] = []
        for (name, _), item in zip(requests, payload.get("results", [])):
            try:
                results.append(_unwrap(name, item))
            except ObsRequestError as exc:
                results.append(exc)
        # haltOnFailure truncates the result list; pad so callers can index by position.
        while len(results) < len(requests):
            name = requests[len(results)][0]
            results.append(ObsRequestError(name, 0, "skipped after an earlier failure"))
        return results

    def subscribe(self, event_type: str, callback: EventCallback) -> Callable[[], None]:
        """Call ``callback(eventData)`` for each ``event_type`` (``"*"`` for all). Returns an unsubscriber.

        Callbacks run on a dispatcher thread, so they may call back into the client."""
        with self._listeners_lock:
            self._listeners.setdefault(event_type, []).append(callback)
            if self._dispatcher is None:
                self._dispatcher = threading.Thread(target=self._dispatch_loop, name="onair-obs-events",
                                                    daemon=True)
                self._dispatcher.start()

        def unsubscribe() -> None:
            with self._listeners_lock:
                callbacks = self._listeners.get(event_type, [])
                if callback in callbacks:
                    callbacks.remove(callback)

        return unsubscribe

    def hold(self, held: bool = True) -> None:
        """While held, a dropped connection is re-established in the background (events keep flowing)."""
        self._held = held
        if held and not self.connected:
            self._start_reconnector()

    def version(self) -> dict[str, Any]:
        """The Hello payload of the live connection (connects if needed)."""
        return self._connection().version

    def close(self) -> None:
        self._closed = True
        self._held = False
        self._wake.set()
        self._events.put(None)
        with self._conn_lock:
            conn, self._conn = self._conn, None
        if conn:
            conn.alive = False
            conn.transport.close()
        self._fail_pending(ObsUnavailable("OBS connection closed"))

    # ------------------------------------------------------------------ request plumbing

    def _roundtrip(self, op: int, body: dict[str, Any], timeout: float, *, label: str) -> dict[str, Any]:
        last: Exception = ObsUnavailable("OBS is not reachable")
        for attempt in range(2):  # one retry, only when the request never left this process
            conn = self._connection()
            request_id = f"onair-{next(self._ids)}"
            pending = _Pending()
            with self._pending_lock:
                self._pending[request_id] = pending
            frame = json.dumps({"op": op, "d": {**body, "requestId": request_id}})
            try:
                with conn.send_lock:
                    conn.transport.send(frame)
            except TransportClosed as exc:
                self._forget(request_id)
                self._drop(conn, exc)
                last = ObsUnavailable("Lost the connection to OBS")
                continue
            if not pending.done.wait(timeout):
                self._forget(request_id)
                raise ObsTimeout(f"OBS did not answer {label} within {timeout:g}s")
            if pending.error is not None:
                raise pending.error
            assert pending.payload is not None
            return pending.payload
        raise last

    def _forget(self, request_id: str) -> None:
        with self._pending_lock:
            self._pending.pop(request_id, None)

    def _fail_pending(self, error: Exception) -> None:
        with self._pending_lock:
            waiting, self._pending = list(self._pending.values()), {}
        for pending in waiting:
            pending.error = error
            pending.done.set()

    # ------------------------------------------------------------------ connection lifecycle

    def _connection(self) -> _Connection:
        conn = self._conn
        if conn and conn.alive:
            return conn
        with self._conn_lock:
            conn = self._conn
            if conn and conn.alive:
                return conn
            if self._closed:
                raise ObsUnavailable("The OBS client is shut down")
            self._conn = conn = self._open()
            return conn

    def _open(self) -> _Connection:
        endpoint = self._endpoint()
        transport = self._connector(endpoint, self._connect_timeout)
        try:
            hello = self._expect(transport, OP_HELLO)
            identify: dict[str, Any] = {"rpcVersion": RPC_VERSION, "eventSubscriptions": EVENT_SUBSCRIPTIONS}
            challenge = hello.get("authentication")
            if challenge:
                if not endpoint.password:
                    raise ObsAuthError("OBS asks for a password and none is set")
                identify["authentication"] = auth_response(
                    endpoint.password, challenge["salt"], challenge["challenge"])
            transport.send(json.dumps({"op": OP_IDENTIFY, "d": identify}))
            try:
                self._expect(transport, OP_IDENTIFIED)
            except TransportClosed as exc:
                if challenge or exc.code == CLOSE_AUTH_FAILED:
                    raise ObsAuthError("OBS rejected the password") from exc
                raise
        except TransportClosed as exc:
            transport.close()
            raise ObsUnavailable(f"OBS closed the connection during the handshake ({exc})") from exc
        except TimeoutError as exc:
            transport.close()
            raise ObsUnavailable("OBS did not finish the handshake in time") from exc
        except BaseException:
            transport.close()
            raise
        conn = _Connection(transport, hello)
        threading.Thread(target=self._read_loop, args=(conn,), name="onair-obs-reader", daemon=True).start()
        logger.info("Connected to OBS %s", hello.get("obsStudioVersion") or hello.get("obsWebSocketVersion"))
        return conn

    def _expect(self, transport: Transport, op: int) -> dict[str, Any]:
        message = json.loads(transport.recv(timeout=self._connect_timeout))
        if message.get("op") != op:
            raise ObsUnavailable(f"Unexpected handshake message from OBS (op {message.get('op')})")
        return message.get("d") or {}

    def _read_loop(self, conn: _Connection) -> None:
        reason: Exception = TransportClosed()
        while conn.alive:
            try:
                raw = conn.transport.recv(timeout=None)
            except TimeoutError:
                continue
            except TransportClosed as exc:
                reason = exc
                break
            except Exception as exc:  # a broken frame must not kill the process, only this socket
                reason = exc
                break
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            op, data = message.get("op"), message.get("d") or {}
            if op in (OP_RESPONSE, OP_BATCH_RESPONSE):
                with self._pending_lock:
                    pending = self._pending.pop(data.get("requestId", ""), None)
                if pending:
                    pending.payload = data
                    pending.done.set()
            elif op == OP_EVENT:
                self._events.put(data)
        self._drop(conn, reason)

    def _drop(self, conn: _Connection, reason: Exception) -> None:
        """Retire ``conn`` (idempotent). A newer connection is never touched."""
        if not conn.alive:
            return
        conn.alive = False
        conn.transport.close()
        with self._conn_lock:
            if self._conn is conn:
                self._conn = None
        if not self._closed:
            logger.info("OBS connection lost: %s", reason)
        self._fail_pending(ObsUnavailable("Lost the connection to OBS"))
        self._events.put({"eventType": "OnAirDisconnected", "eventData": {}})
        if self._held and not self._closed:
            self._start_reconnector()

    def _start_reconnector(self) -> None:
        with self._conn_lock:
            if self._reconnector and self._reconnector.is_alive():
                return
            self._reconnector = threading.Thread(target=self._reconnect_loop, name="onair-obs-reconnect",
                                                 daemon=True)
            self._reconnector.start()

    def _reconnect_loop(self) -> None:
        delay = 1.0
        while self._held and not self._closed and not self.connected:
            try:
                self._connection()
                self._events.put({"eventType": "OnAirReconnected", "eventData": {}})
                return
            except ObsAuthError:
                return  # a wrong password will not fix itself; the next explicit call reports it
            except Exception:
                self._wake.wait(delay)
                delay = min(delay * 2, 30.0)

    # ------------------------------------------------------------------ events

    def _dispatch_loop(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                return
            name = event.get("eventType", "")
            data = event.get("eventData") or {}
            with self._listeners_lock:
                # "*" listeners get the event name alongside its data.
                calls = [(cb, data) for cb in self._listeners.get(name, ())]
                calls += [(cb, {"eventType": name, **data}) for cb in self._listeners.get("*", ())]
            for callback, argument in calls:
                try:
                    callback(argument)
                except Exception:
                    logger.exception("OBS event listener failed for %s", name)


def _unwrap(request_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("requestStatus") or {}
    if not status.get("result", False):
        raise ObsRequestError(request_type, int(status.get("code", 0)), status.get("comment", ""))
    return payload.get("responseData") or {}
