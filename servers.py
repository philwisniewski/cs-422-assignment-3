"""
Parse the server list exported from https://iperf3serverlist.net/.

Expected columns (header names are matched loosely, case-insensitively):
    IP/HOST, PORT, GB/S, CONTINENT, COUNTRY, SITE, PROVIDER
The delimiter (comma, tab, semicolon) is auto-detected. The host cell may also
contain a full command such as "iperf3 -c host -p 5201-5210", and the port
cell may be a single port, a range "5201-5210", or a list "5201,5202".
A minimal "host,port" CSV also works.
"""

import csv
import io
import re


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def parse_ports(text):
    ports = []
    for part in re.split(r"[,\s;/]+", (text or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and b - a <= 100:
                ports.extend(range(a, b + 1))
        elif part.isdigit():
            ports.append(int(part))
    return [p for p in ports if 0 < p < 65536]


def parse_host_cell(cell):
    """Return (host, ports-from-cell)."""
    cell = (cell or "").strip()
    tokens = cell.split()
    host, ports = None, []
    if "-c" in tokens:
        i = tokens.index("-c")
        host = tokens[i + 1] if i + 1 < len(tokens) else None
    elif tokens:
        host = tokens[0]
    if "-p" in tokens:
        i = tokens.index("-p")
        if i + 1 < len(tokens):
            ports = parse_ports(tokens[i + 1])
    if host:
        host = host.strip()
        if host.startswith("[") and "]" in host:          # [v6]:port
            inner, _, rest = host[1:].partition("]")
            host = inner
            if rest.startswith(":"):
                ports = ports or parse_ports(rest[1:])
        elif host.count(":") == 1:                          # host:port
            h, _, p = host.partition(":")
            host = h
            ports = ports or parse_ports(p)
    return host, ports


def load_servers(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return []

    header = [_norm(c) for c in rows[0]]
    has_header = any(h in ("iphost", "host", "ip", "port", "server", "address") or "host" in h
                     for h in header)

    def find(*keys):
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                return i
        return None

    if has_header:
        body = rows[1:]
        c_host = find("iphost", "host", "server", "address", "ip")
        c_port = find("port")
        c_speed = find("gbs", "gbps", "speed", "bandwidth")
        c_cont = find("continent")
        c_country = find("country")
        c_site = find("site", "city", "location")
        c_prov = find("provider", "isp", "operator")
    else:
        body = rows
        c_host, c_port = 0, (1 if len(rows[0]) > 1 else None)
        c_speed = c_cont = c_country = c_site = c_prov = None

    def get(r, c):
        return r[c].strip() if c is not None and c < len(r) else ""

    servers, seen = [], set()
    for r in body:
        host, ports = parse_host_cell(get(r, c_host))
        if not host:
            continue
        ports = parse_ports(get(r, c_port)) or ports or [5201]
        key = (host.lower(), tuple(ports))
        if key in seen:
            continue
        seen.add(key)
        servers.append({
            "host": host, "ports": ports,
            "gbps": get(r, c_speed), "continent": get(r, c_cont),
            "country": get(r, c_country), "site": get(r, c_site),
            "provider": get(r, c_prov),
        })
    return servers


def is_ipv6_literal(host):
    return ":" in host
