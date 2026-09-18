"""Wake-on-LAN support.

Sends a standard Magic Packet (UDP broadcast to port 9) to wake a sleeping PC
on the local network. The target PC must have Wake-on-LAN enabled in its NIC
settings and BIOS. On the sleeping machine, nothing runs here — WoL packets
are consumed by the network card.

Since this app IS the desktop app, WoL is normally used to re-wake a machine
that another Ritual-controlled device put to sleep, or as an architectural
building block for remote/mesh wake. We provide the sender and a small store
of known hardware addresses so it's ready to use.
"""
from __future__ import annotations

import re
import socket

# Separators Windows/Linux/router UIs put between the octets: 'aa:bb:..',
# 'AA-BB-..', 'aabb.ccdd.eeff', 'aabbccddeeff' and 'aa bb cc dd ee ff'.
_MAC_SEPARATORS = re.compile(r"[\s:.\-]")

MAGIC_PACKET_BYTES = 102


class WakeOnLan:
    """Send Wake-on-LAN magic packets."""

    def __init__(self, broadcast_ip: str = "255.255.255.255",
                 port: int = 9) -> None:
        self.broadcast_ip = broadcast_ip
        self.port = port

    @staticmethod
    def _parse_mac(mac: str) -> bytes:
        """Accept MAC as 'aa:bb:cc:dd:ee:ff', 'aabb.ccdd.eeff', hex or spaces.

        Whitespace is a separator too: `getmac` and most router UIs print
        'AA BB CC DD EE FF', and the previous version only stripped the other
        three separators, so that (perfectly valid) address was rejected as
        "must be 6 bytes".
        """
        cleaned = _MAC_SEPARATORS.sub("", str(mac or "").strip().upper())
        if len(cleaned) != 12:
            raise ValueError(
                "MAC address must be 6 bytes (e.g. AA:BB:CC:DD:EE:FF)")
        try:
            return bytes(int(cleaned[i:i + 2], 16) for i in range(0, 12, 2))
        except ValueError as e:
            raise ValueError("invalid hex in MAC address") from e

    @classmethod
    def _magic_packet(cls, mac: str) -> bytes:
        """Build the 102-byte magic packet: 6x0xFF then 16x the 6-byte MAC.

        Shared by `send` and `send_unicast` so the byte layout (and the length
        check) can never diverge between the two transports.
        """
        mac_bytes = cls._parse_mac(mac)
        payload = b"\xff" * 6 + mac_bytes * 16
        if len(payload) != MAGIC_PACKET_BYTES:
            raise ValueError("malformed magic packet")
        return payload

    def send(self, mac: str) -> None:
        """Broadcast a magic packet to wake the device with `mac`."""
        payload = self._magic_packet(mac)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(payload, (self.broadcast_ip, self.port))

    def send_unicast(self, mac: str, ip: str) -> None:
        """Send a magic packet to a specific IP (useful behind some firewalls)."""
        payload = self._magic_packet(mac)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(payload, (ip, self.port))


def is_local_port_available(host: str, port: int) -> bool:
    """Check whether a TCP port is free on the given host (for diagnostics)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return False  # something is listening
        except OSError:
            return True