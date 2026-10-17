"""ONVIF camera discovery via WS-Discovery (UDP multicast 239.255.255.250:3702).
Standard library only. Needs the hub on the same LAN segment as the cameras
(in Docker: network_mode: host)."""

import asyncio
import socket
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse

MULTICAST = ("239.255.255.250", 3702)

PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>uuid:{msg_id}</w:MessageID>
  <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""


def parse_probe_match(payload: bytes, sender_ip: str) -> dict | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    match = root.find(".//{*}ProbeMatch")
    if match is None:
        return None
    xaddrs = (match.findtext("{*}XAddrs") or "").split()
    scopes = (match.findtext("{*}Scopes") or "").split()

    def scope(kind: str) -> str | None:
        prefix = f"onvif://www.onvif.org/{kind}/"
        for s in scopes:
            if s.startswith(prefix):
                return unquote(s[len(prefix):]).replace("_", " ")
        return None

    xaddr = xaddrs[0] if xaddrs else None
    address = urlparse(xaddr).hostname if xaddr else sender_ip
    return {
        "address": address or sender_ip,
        "xaddr": xaddr,
        "name": scope("name"),
        "manufacturer": scope("mfr") or scope("manufacturer"),
        "model": scope("hardware"),
    }


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, dict] = {}

    def datagram_received(self, data: bytes, addr) -> None:
        cam = parse_probe_match(data, addr[0])
        if cam:
            self.found[cam["address"]] = cam


async def discover(timeout: float = 4.0) -> list[dict]:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", 0))
    transport, protocol = await loop.create_datagram_endpoint(_Collector, sock=sock)
    try:
        probe = PROBE.format(msg_id=uuid.uuid4()).encode()
        for _ in range(2):  # UDP is lossy; send twice
            transport.sendto(probe, MULTICAST)
            await asyncio.sleep(0.3)
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return sorted(protocol.found.values(), key=lambda c: c["address"])
