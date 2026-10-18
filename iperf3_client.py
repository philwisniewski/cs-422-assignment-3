"""
A from-scratch iperf3-compatible TCP client (sender / "upload" direction).

It speaks the iperf3 control protocol directly over sockets; it never invokes
the iperf3 binary. Protocol summary (see iperf3 src/iperf_api.c, iperf.h):

    client                                   server (stock iperf3 -s)
    ------                                   ------
    connect control socket (port 5201)
    send 37-byte cookie (36 chars + NUL) --->
                                       <---  state byte PARAM_EXCHANGE (9)
                                             (or ACCESS_DENIED (-1) if busy)
    send params JSON (4-byte BE length + JSON) --->
                                       <---  CREATE_STREAMS (10)
                                             (or SERVER_ERROR (-2) + 2x int32
                                              i_errno/errno if it rejects them)
    connect data socket, send same cookie --->
                                       <---  TEST_START (1), TEST_RUNNING (2)
    ===== send bulk data on data socket for `duration` seconds =====
    send TEST_END (4)                  --->
                                       <---  EXCHANGE_RESULTS (13)
    send client results JSON           --->
                                       <---  server results JSON
                                       <---  DISPLAY_RESULTS (14)
    send IPERF_DONE (16), close        --->

While data flows, TCP_INFO is read from the *data* socket every `interval`
seconds (see tcp_info.py) and goodput is computed from the change in
tcpi_bytes_acked over the actually elapsed sampling time.

On any failure after the control connection is up, the client sends
CLIENT_TERMINATE (12) so that the public server is released immediately
instead of staying "busy" until its own timeout.
"""

import json
import os
import random
import select
import socket
import struct
import time

from tcp_info import get_tcp_info, get_congestion_control, set_congestion_control

# --- iperf3 control-channel states (src/iperf.h) -------------------------
TEST_START = 1
TEST_RUNNING = 2
TEST_END = 4
PARAM_EXCHANGE = 9
CREATE_STREAMS = 10
SERVER_TERMINATE = 11
CLIENT_TERMINATE = 12
EXCHANGE_RESULTS = 13
DISPLAY_RESULTS = 14
IPERF_START = 15
IPERF_DONE = 16
ACCESS_DENIED = -1
SERVER_ERROR = -2

STATE_NAMES = {
    TEST_START: "TEST_START", TEST_RUNNING: "TEST_RUNNING", TEST_END: "TEST_END",
    PARAM_EXCHANGE: "PARAM_EXCHANGE", CREATE_STREAMS: "CREATE_STREAMS",
    SERVER_TERMINATE: "SERVER_TERMINATE", CLIENT_TERMINATE: "CLIENT_TERMINATE",
    EXCHANGE_RESULTS: "EXCHANGE_RESULTS", DISPLAY_RESULTS: "DISPLAY_RESULTS",
    IPERF_START: "IPERF_START", IPERF_DONE: "IPERF_DONE",
    ACCESS_DENIED: "ACCESS_DENIED", SERVER_ERROR: "SERVER_ERROR",
}

COOKIE_SIZE = 37                       # 36 random chars + NUL terminator
COOKIE_CHARS = "abcdefghijklmnopqrstuvwxyz234567"
MAX_JSON_LEN = 1 << 20                 # sanity bound on server JSON length
CLIENT_VERSION = "3.16-py"

# Error kinds -> whether retrying the same server later makes sense.
RETRYABLE = {
    "busy": True,                # server is running someone else's test
    "connect_timeout": True,     # non-responsive / overloaded
    "connect_refused": True,     # (try another port of the range)
    "connect_error": False,      # unreachable network, no IPv6, ...
    "dns": False,
    "ctrl_timeout": True,        # server stopped answering on control channel
    "premature_close": True,     # connection reset / closed mid-test
    "stall": True,               # no bytes acknowledged for stall_timeout
    "server_error": True,        # iperf3 SERVER_ERROR (often transient)
    "server_terminate": True,
    "param_rejected": False,     # server rejected parameter negotiation
    "protocol": False,           # unexpected message / not an iperf3 server
    "cc_unavailable": False,     # local kernel cannot use the requested CC
    "interrupted": False,
}


class Iperf3Error(Exception):
    def __init__(self, kind, phase, message):
        super().__init__("[%s during %s] %s" % (kind, phase, message))
        self.kind = kind
        self.phase = phase
        self.message = message

    @property
    def retryable(self):
        return RETRYABLE.get(self.kind, False)


def make_cookie():
    rnd = random.SystemRandom()
    return ("".join(rnd.choice(COOKIE_CHARS) for _ in range(COOKIE_SIZE - 1))
            .encode("ascii") + b"\x00")


class Iperf3Client:
    """Runs one TCP upload test against one iperf3 server.

    Use ``run()``; it never raises for network/protocol problems. It returns a
    result dict with ``status`` in {"ok", "incomplete_termination", "failed"}
    plus any samples collected (partial on mid-test failure).
    """

    def __init__(self, host, port, duration=60.0, interval=0.2, congestion=None,
                 connect_timeout=5.0, ctrl_timeout=15.0, stall_timeout=15.0,
                 blksize=128 * 1024, ipv4_only=False, log=None):
        self.host = host
        self.port = int(port)
        self.duration = float(duration)
        self.interval = float(interval)
        self.congestion = congestion
        self.connect_timeout = connect_timeout
        self.ctrl_timeout = ctrl_timeout
        self.stall_timeout = stall_timeout
        self.blksize = int(blksize)
        self.ipv4_only = ipv4_only
        self.log = log or (lambda msg: None)

        self.cookie = make_cookie()
        self.ctrl = None
        self.data = None
        self.phase = "init"
        self.samples = []
        self.state_trace = []
        self.app_bytes_sent = 0
        self.t0 = None

    # ------------------------------------------------------------------ utils
    def _trace(self, direction, state):
        self.state_trace.append({
            "t_unix": round(time.time(), 6), "dir": direction,
            "state": STATE_NAMES.get(state, str(state))})

    def _resolve(self):
        try:
            infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM,
                                       socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise Iperf3Error("dns", "resolve", str(e))
        if self.ipv4_only:
            infos = [i for i in infos if i[0] == socket.AF_INET]
        # Prefer IPv4: many hosts/containers have no IPv6 route.
        infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
        if not infos:
            raise Iperf3Error("dns", "resolve", "no usable address for %s" % self.host)
        return infos

    def _connect(self, family, sockaddr, congestion=None):
        s = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            if congestion:
                # Must be selected before connect() so that the whole
                # connection (incl. slow start) uses this algorithm.
                try:
                    set_congestion_control(s, congestion)
                except OSError as e:
                    raise Iperf3Error(
                        "cc_unavailable", self.phase,
                        "cannot select congestion control %r: %s (is the module "
                        "loaded and allowed? see README)" % (congestion, e))
            s.settimeout(self.connect_timeout)
            try:
                s.connect(sockaddr)
            except socket.timeout:
                raise Iperf3Error("connect_timeout", self.phase,
                                  "connect to %s timed out after %.1fs"
                                  % (sockaddr[0], self.connect_timeout))
            except ConnectionRefusedError as e:
                raise Iperf3Error("connect_refused", self.phase, str(e))
            except OSError as e:
                raise Iperf3Error("connect_error", self.phase, str(e))
            return s
        except BaseException:
            s.close()
            raise

    def _recv_exact(self, sock, n, timeout=None):
        sock.settimeout(self.ctrl_timeout if timeout is None else timeout)
        chunks = []
        remaining = n
        while remaining:
            try:
                chunk = sock.recv(remaining)
            except socket.timeout:
                raise Iperf3Error("ctrl_timeout", self.phase,
                                  "no reply from server within %.1fs" % sock.gettimeout())
            except (ConnectionResetError, BrokenPipeError) as e:
                raise Iperf3Error("premature_close", self.phase, str(e))
            except OSError as e:
                raise Iperf3Error("premature_close", self.phase, str(e))
            if not chunk:
                raise Iperf3Error("premature_close", self.phase,
                                  "server closed the control connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_all(self, sock, payload):
        sock.settimeout(self.ctrl_timeout)
        try:
            sock.sendall(payload)
        except socket.timeout:
            raise Iperf3Error("ctrl_timeout", self.phase, "send timed out")
        except OSError as e:
            raise Iperf3Error("premature_close", self.phase, "send failed: %s" % e)

    def _send_state(self, state):
        self._trace("tx", state)
        self._send_all(self.ctrl, struct.pack("b", state))

    def _read_server_error(self):
        """After SERVER_ERROR the server sends i_errno and errno (int32, BE)."""
        try:
            raw = self._recv_exact(self.ctrl, 8, timeout=3.0)
            i_errno, sys_errno = struct.unpack("!ii", raw)
            msg = "iperf3 server error i_errno=%d" % i_errno
            if sys_errno:
                msg += " errno=%d (%s)" % (sys_errno, os.strerror(sys_errno))
            return msg
        except Iperf3Error:
            return "iperf3 server error (no error code received)"

    def _read_state(self, timeout=None):
        b = self._recv_exact(self.ctrl, 1, timeout)
        state = struct.unpack("b", b)[0]
        self._trace("rx", state)
        if state == ACCESS_DENIED:
            raise Iperf3Error("busy", self.phase,
                              "server answered ACCESS_DENIED (busy with another test "
                              "or refusing this client)")
        if state == SERVER_ERROR:
            raise Iperf3Error("server_error", self.phase, self._read_server_error())
        if state == SERVER_TERMINATE:
            raise Iperf3Error("server_terminate", self.phase,
                              "server sent SERVER_TERMINATE")
        return state

    def _expect_state(self, expected, timeout=None):
        state = self._read_state(timeout)
        if state != expected:
            raise Iperf3Error("protocol", self.phase, "expected %s, got %s" % (
                STATE_NAMES[expected], STATE_NAMES.get(state, state)))

    def _send_json(self, obj):
        payload = json.dumps(obj, separators=(",", ":")).encode()
        self._send_all(self.ctrl, struct.pack("!I", len(payload)) + payload)

    def _recv_json(self):
        (length,) = struct.unpack("!I", self._recv_exact(self.ctrl, 4))
        if length == 0 or length > MAX_JSON_LEN:
            raise Iperf3Error("protocol", self.phase, "bad JSON length %d" % length)
        raw = self._recv_exact(self.ctrl, length)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as e:
            raise Iperf3Error("protocol", self.phase, "bad JSON from server: %s" % e)

    # ------------------------------------------------------------- sampling
    def _sample(self, prev):
        """Read TCP_INFO from the data socket and build one log row."""
        info, raw_len = get_tcp_info(self.data)
        t = time.monotonic()
        row = {
            "t_s": t - self.t0,
            "timestamp_unix": time.time(),
            "tcp_info_len_B": raw_len,
            "ca_state": info.get("ca_state"),
            "snd_cwnd_seg": info.get("snd_cwnd"),
            "snd_ssthresh_seg": info.get("snd_ssthresh"),
            "snd_mss_B": info.get("snd_mss"),
            "srtt_us": info.get("rtt"),
            "rttvar_us": info.get("rttvar"),
            "min_rtt_us": info.get("min_rtt"),
            "rto_us": info.get("rto"),
            "unacked_seg": info.get("unacked"),
            "sacked_seg": info.get("sacked"),
            "lost_seg": info.get("lost"),
            "retrans_inflight_seg": info.get("retrans"),
            "total_retrans_seg": info.get("total_retrans"),
            "total_rto": info.get("total_rto"),
            "bytes_acked_B": info.get("bytes_acked"),
            "bytes_sent_B": info.get("bytes_sent"),
            "bytes_retrans_B": info.get("bytes_retrans"),
            "delivered_seg": info.get("delivered"),
            "app_bytes_sent_B": self.app_bytes_sent,
            "notsent_bytes_B": info.get("notsent_bytes"),
            "pacing_rate_Bps": info.get("pacing_rate"),
            "delivery_rate_Bps": info.get("delivery_rate"),
            "delivery_rate_app_limited": info.get("delivery_rate_app_limited"),
            "busy_time_us": info.get("busy_time"),
            "rwnd_limited_us": info.get("rwnd_limited"),
            "sndbuf_limited_us": info.get("sndbuf_limited"),
            "snd_wnd_B": info.get("snd_wnd"),
        }
        if row["snd_cwnd_seg"] is not None and row["snd_mss_B"]:
            row["cwnd_B"] = row["snd_cwnd_seg"] * row["snd_mss_B"]
        else:
            row["cwnd_B"] = None

        # Interval quantities: differences of cumulative counters divided by
        # the *measured* elapsed time between the two getsockopt() calls.
        def delta(key):
            if prev is None or row.get(key) is None or prev.get(key) is None:
                return None
            return row[key] - prev[key]

        dt = None if prev is None else row["t_s"] - prev["t_s"]
        d_acked = delta("bytes_acked_B")
        row["dt_s"] = dt
        row["d_bytes_acked_B"] = d_acked
        row["goodput_bps"] = (d_acked * 8.0 / dt) if (dt and d_acked is not None) else None
        row["d_retrans_seg"] = delta("total_retrans_seg")
        row["d_bytes_retrans_B"] = delta("bytes_retrans_B")
        row["d_bytes_sent_B"] = delta("bytes_sent_B")
        row["d_total_rto"] = delta("total_rto")
        return row

    # --------------------------------------------------------- data transfer
    def _transfer(self):
        """Send continuously for self.duration, sampling every self.interval."""
        self.phase = "running"
        payload = memoryview(os.urandom(self.blksize))
        self.data.setblocking(False)
        self.ctrl.setblocking(False)

        self.t0 = time.monotonic()
        end = self.t0 + self.duration
        prev = self._sample(None)            # baseline at t = 0
        prev["sample_idx"] = 0
        self.samples.append(prev)
        next_sample = self.t0 + self.interval
        last_progress = self.t0
        last_acked = prev["bytes_acked_B"] or 0

        while True:
            now = time.monotonic()
            if now >= next_sample or now >= end:
                row = self._sample(prev)
                row["sample_idx"] = len(self.samples)
                self.samples.append(row)
                prev = row
                acked = row["bytes_acked_B"] or 0
                if acked > last_acked:
                    last_acked, last_progress = acked, now
                elif now - last_progress > self.stall_timeout:
                    raise Iperf3Error("stall", self.phase,
                                      "no data acknowledged for %.1fs" % self.stall_timeout)
                if now >= end:
                    break
                while next_sample <= now:
                    next_sample += self.interval

            deadline = min(next_sample, end)
            timeout = max(0.0, deadline - time.monotonic())
            try:
                readable, writable, _ = select.select([self.ctrl], [self.data], [], timeout)
            except InterruptedError:
                continue

            if readable:
                self._check_ctrl_during_test()

            if writable:
                for _ in range(512):
                    try:
                        n = self.data.send(payload)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError as e:
                        raise Iperf3Error("premature_close", self.phase,
                                          "data connection failed: %s" % e)
                    self.app_bytes_sent += n
                    if time.monotonic() >= deadline:
                        break

        self.ctrl.setblocking(True)

    def _check_ctrl_during_test(self):
        """The server should be silent while the test runs; anything else is
        an error report or a close."""
        try:
            b = self.ctrl.recv(1)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            raise Iperf3Error("premature_close", self.phase, "control socket: %s" % e)
        if not b:
            raise Iperf3Error("premature_close", self.phase,
                              "server closed the control connection during the test")
        state = struct.unpack("b", b)[0]
        self._trace("rx", state)
        self.ctrl.setblocking(True)
        if state == SERVER_ERROR:
            raise Iperf3Error("server_error", self.phase, self._read_server_error())
        if state == SERVER_TERMINATE:
            raise Iperf3Error("server_terminate", self.phase, "server sent SERVER_TERMINATE")
        if state == ACCESS_DENIED:
            raise Iperf3Error("busy", self.phase, "ACCESS_DENIED during test")
        self.ctrl.setblocking(False)
        self.log("  ! unexpected state %s from server during test (ignored)"
                 % STATE_NAMES.get(state, state))

    # ------------------------------------------------------------- results
    def _client_results(self, cpu0, wall0):
        cpu1 = os.times()
        wall = max(time.monotonic() - wall0, 1e-9)
        user = (cpu1.user - cpu0.user) / wall * 100.0
        system = (cpu1.system - cpu0.system) / wall * 100.0
        last = self.samples[-1] if self.samples else {}
        return {
            "cpu_util_total": user + system,
            "cpu_util_user": user,
            "cpu_util_system": system,
            "sender_has_retransmits": 1,
            "congestion_used": self.actual_cc or "",
            "streams": [{
                "id": 1,                     # iperf3 numbers the first stream 1
                "bytes": self.app_bytes_sent,
                "retransmits": last.get("total_retrans_seg") or 0,
                "jitter": 0,
                "errors": 0,
                "omitted_errors": 0,
                "packets": 0,
                "omitted_packets": 0,
                "start_time": 0,
                "end_time": last.get("t_s", self.duration),
            }],
        }

    # ------------------------------------------------------------------ run
    def run(self):
        result = {
            "host": self.host, "port": self.port, "address": None,
            "requested_cc": self.congestion, "actual_cc": None,
            "duration_requested_s": self.duration, "interval_s": self.interval,
            "start_unix": time.time(), "status": "failed",
            "error_kind": None, "error_phase": None, "error_msg": None,
            "server_results": None, "client_results": None,
        }
        self.actual_cc = None
        cpu0, wall0 = os.times(), time.monotonic()
        transfer_done = False
        try:
            # 1. control connection (try each resolved address)
            self.phase = "resolve"
            infos = self._resolve()
            self.phase = "control_connect"
            last_err = None
            for family, _, _, _, sockaddr in infos:
                try:
                    self.ctrl = self._connect(family, sockaddr)
                    self.family, self.sockaddr = family, sockaddr
                    break
                except Iperf3Error as e:
                    last_err = e
            if self.ctrl is None:
                raise last_err
            result["address"] = self.sockaddr[0]
            self.ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            # 2. cookie. A busy server may already have written ACCESS_DENIED
            #    and closed, so a failed send is followed by a read attempt.
            self.phase = "cookie"
            try:
                self._send_all(self.ctrl, self.cookie)
            except Iperf3Error:
                self._read_state(timeout=2.0)
                raise

            # 3. parameter exchange
            self.phase = "param_exchange"
            self._expect_state(PARAM_EXCHANGE)
            params = {
                "tcp": True, "omit": 0, "time": int(round(self.duration)),
                "num": 0, "blockcount": 0, "parallel": 1, "len": self.blksize,
                "pacing_timer": 1000, "client_version": CLIENT_VERSION,
            }
            # NOTE: we deliberately do NOT send "congestion": stock servers
            # would try to apply it to their *receiving* socket and abort the
            # test if their kernel lacks it. Only the sender's CC matters here.
            self._send_json(params)
            result["params_sent"] = params

            # 4. server accepts parameters -> CREATE_STREAMS
            self.phase = "create_streams"
            try:
                self._expect_state(CREATE_STREAMS)
            except Iperf3Error as e:
                if e.kind in ("server_error", "busy", "premature_close", "protocol"):
                    raise Iperf3Error("param_rejected", self.phase,
                                      "server rejected parameters: " + e.message)
                raise

            # 5. data connection to the same address, CC chosen before connect
            self.phase = "data_connect"
            self.data = self._connect(self.family, self.sockaddr, self.congestion)
            self.actual_cc = get_congestion_control(self.data)
            result["actual_cc"] = self.actual_cc
            self._send_all(self.data, self.cookie)

            # 6. TEST_START then TEST_RUNNING
            self.phase = "test_start"
            self._expect_state(TEST_START)
            self._expect_state(TEST_RUNNING)

            # 7. transfer + TCP_INFO sampling
            self._transfer()
            transfer_done = True

            # 8. terminate following iperf3 semantics
            self.phase = "test_end"
            self._send_state(TEST_END)
            self.phase = "exchange_results"
            self._expect_state(EXCHANGE_RESULTS, timeout=max(self.ctrl_timeout, 30))
            client_res = self._client_results(cpu0, wall0)
            result["client_results"] = client_res
            self._send_json(client_res)
            result["server_results"] = self._recv_json()
            self.phase = "display_results"
            self._expect_state(DISPLAY_RESULTS)
            self._send_state(IPERF_DONE)
            result["status"] = "ok"

        except Iperf3Error as e:
            result.update(error_kind=e.kind, error_phase=e.phase, error_msg=e.message)
            result["status"] = "incomplete_termination" if transfer_done else "failed"
            self._abort()
        except KeyboardInterrupt:
            result.update(error_kind="interrupted", error_phase=self.phase,
                          error_msg="interrupted by user")
            self._abort()
            raise
        except Exception as e:  # never let one bad server crash the experiment
            result.update(error_kind="protocol", error_phase=self.phase,
                          error_msg="%s: %s" % (type(e).__name__, e))
            result["status"] = "incomplete_termination" if transfer_done else "failed"
            self._abort()
        finally:
            for s in (self.data, self.ctrl):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
            result["end_unix"] = time.time()
            result["samples"] = self.samples
            result["app_bytes_sent_B"] = self.app_bytes_sent
            result["duration_actual_s"] = self.samples[-1]["t_s"] if self.samples else 0.0
            result["state_trace"] = self.state_trace
            srv = result.get("server_results") or {}
            try:
                result["server_bytes_received_B"] = sum(
                    st.get("bytes", 0) for st in srv.get("streams", []))
            except (AttributeError, TypeError):
                result["server_bytes_received_B"] = None
        return result

    def _abort(self):
        """Tell the server we are giving up so it frees itself (CLIENT_TERMINATE)."""
        if self.ctrl is None:
            return
        try:
            self.ctrl.setblocking(True)
            self.ctrl.settimeout(1.0)
            self.ctrl.send(struct.pack("b", CLIENT_TERMINATE))
            self._trace("tx", CLIENT_TERMINATE)
        except OSError:
            pass
