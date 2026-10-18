"""
Decoding of Linux ``struct tcp_info`` (getsockopt(IPPROTO_TCP, TCP_INFO)).

The layout below follows include/uapi/linux/tcp.h. The kernel copies
min(len_requested, sizeof(struct tcp_info)) bytes, so older kernels return a
shorter struct. Every field is described by (name, struct-format, byte offset)
and is only decoded when the returned buffer is long enough to contain it, so
the same code works on any kernel from 4.x to 6.x.

All integers are in host byte order (the kernel copies the struct verbatim).

Units (as defined by the kernel):
    rto, ato, rtt, rttvar, min_rtt, rcv_rtt ......... microseconds
    last_data_sent / last_ack_recv ... ............... milliseconds
    snd_cwnd, snd_ssthresh ........................... segments (of snd_mss bytes)
    snd_mss, advmss, pmtu ............................ bytes
    unacked, sacked, lost, retrans, total_retrans .... segments
    pacing_rate, max_pacing_rate, delivery_rate ...... bytes / second
    bytes_acked, bytes_sent, bytes_retrans ........... bytes (cumulative)
    busy_time, rwnd_limited, sndbuf_limited .......... microseconds (cumulative)
    total_rto_time ................................... milliseconds
"""

import socket
import struct

# Python exposes TCP_INFO / TCP_CONGESTION on Linux; fall back to the
# numeric constants from <netinet/tcp.h> just in case.
TCP_INFO = getattr(socket, "TCP_INFO", 11)
TCP_CONGESTION = getattr(socket, "TCP_CONGESTION", 13)

# Ask for more than any current kernel returns; the kernel truncates.
TCP_INFO_REQUEST_LEN = 512

# (name, struct format char, byte offset)
_FIELDS = [
    ("state", "B", 0),
    ("ca_state", "B", 1),
    ("retransmits", "B", 2),       # current consecutive RTO retransmits
    ("probes", "B", 3),
    ("backoff", "B", 4),
    ("options", "B", 5),
    ("wscale", "B", 6),            # snd_wscale:4 | rcv_wscale:4
    ("app_limited_flags", "B", 7), # delivery_rate_app_limited:1, fastopen_client_fail:2
    ("rto", "I", 8),
    ("ato", "I", 12),
    ("snd_mss", "I", 16),
    ("rcv_mss", "I", 20),
    ("unacked", "I", 24),
    ("sacked", "I", 28),
    ("lost", "I", 32),
    ("retrans", "I", 36),
    ("fackets", "I", 40),
    ("last_data_sent", "I", 44),
    ("last_ack_sent", "I", 48),
    ("last_data_recv", "I", 52),
    ("last_ack_recv", "I", 56),
    ("pmtu", "I", 60),
    ("rcv_ssthresh", "I", 64),
    ("rtt", "I", 68),
    ("rttvar", "I", 72),
    ("snd_ssthresh", "I", 76),
    ("snd_cwnd", "I", 80),
    ("advmss", "I", 84),
    ("reordering", "I", 88),
    ("rcv_rtt", "I", 92),
    ("rcv_space", "I", 96),
    ("total_retrans", "I", 100),
    ("pacing_rate", "Q", 104),
    ("max_pacing_rate", "Q", 112),
    ("bytes_acked", "Q", 120),
    ("bytes_received", "Q", 128),
    ("segs_out", "I", 136),
    ("segs_in", "I", 140),
    ("notsent_bytes", "I", 144),
    ("min_rtt", "I", 148),
    ("data_segs_in", "I", 152),
    ("data_segs_out", "I", 156),
    ("delivery_rate", "Q", 160),
    ("busy_time", "Q", 168),
    ("rwnd_limited", "Q", 176),
    ("sndbuf_limited", "Q", 184),
    ("delivered", "I", 192),
    ("delivered_ce", "I", 196),
    ("bytes_sent", "Q", 200),
    ("bytes_retrans", "Q", 208),
    ("dsack_dups", "I", 216),
    ("reord_seen", "I", 220),
    ("rcv_ooopack", "I", 224),
    ("snd_wnd", "I", 228),
    ("rcv_wnd", "I", 232),
    ("rehash", "I", 236),
    ("total_rto", "H", 240),
    ("total_rto_recoveries", "H", 242),
    ("total_rto_time", "I", 244),
]

# Pre-compile one Struct per field ("=" = native byte order, no padding).
_COMPILED = [(name, struct.Struct("=" + fmt), off) for name, fmt, off in _FIELDS]

# Human-readable description of the decoding, written into metadata.json.
FORMAT_DESCRIPTION = [
    {"field": name, "ctype": {"B": "__u8", "H": "__u16", "I": "__u32", "Q": "__u64"}[fmt],
     "offset": off}
    for name, fmt, off in _FIELDS
]

TCP_CA_STATES = {0: "Open", 1: "Disorder", 2: "CWR", 3: "Recovery", 4: "Loss"}


def decode_tcp_info(buf):
    """Decode a raw tcp_info buffer into a dict (only fields present in buf)."""
    out = {}
    n = len(buf)
    for name, st, off in _COMPILED:
        if off + st.size <= n:
            out[name] = st.unpack_from(buf, off)[0]
    if "wscale" in out:
        out["snd_wscale"] = out["wscale"] & 0x0F
        out["rcv_wscale"] = out["wscale"] >> 4
    if "app_limited_flags" in out:
        out["delivery_rate_app_limited"] = out["app_limited_flags"] & 0x01
    return out


def get_tcp_info(sock):
    """Return (decoded dict, raw length) for a connected TCP socket."""
    buf = sock.getsockopt(socket.IPPROTO_TCP, TCP_INFO, TCP_INFO_REQUEST_LEN)
    return decode_tcp_info(buf), len(buf)


def get_congestion_control(sock):
    """Return the congestion-control algorithm actually in use on sock."""
    raw = sock.getsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, 16)
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace")


def set_congestion_control(sock, algo):
    """Select a congestion-control algorithm (must be done before connect())."""
    sock.setsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, algo.encode("ascii"))
