from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .errors import ToolkitError
from .serial_cli import DiagnosticConsole


HOSTNAME_RE = re.compile(
    r"^(?=.{1,63}$)[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$"
)
KEY_VALUE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")


@dataclass(frozen=True)
class NetworkSnapshot:
    runtime: dict[str, str]
    identity: dict[str, str]
    active: dict[str, str]
    staged: dict[str, str] | None
    raw: str

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime": self.runtime,
            "identity": self.identity,
            "active": self.active,
            "staged": self.staged,
            "raw": self.raw,
        }


def validate_hostname(hostname: str) -> str:
    value = hostname.strip()
    if not HOSTNAME_RE.fullmatch(value):
        raise ToolkitError(
            "hostname must be 1-63 letters, digits, or hyphens and cannot "
            "start or end with a hyphen"
        )
    return value


def validate_ipv4(value: str, field: str, *, optional: bool = False) -> str:
    value = value.strip()
    if optional and value.lower() in {"", "-", "none"}:
        return "-"
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise ToolkitError(f"{field} is not a valid IPv4 address: {value}") from exc


def validate_dns(value: str, field: str) -> str:
    value = validate_ipv4(value, field, optional=True)
    if value != "-":
        first_octet = ipaddress.IPv4Address(value).packed[0]
        # Firmware accepts link-local DNS, but excludes non-unicast ranges.
        if first_octet in {0, 127} or first_octet >= 224:
            raise ToolkitError(f"{field} must be a unicast IPv4 address")
    return value


def validate_netmask(value: str) -> str:
    value = validate_ipv4(value, "netmask")
    try:
        network = ipaddress.IPv4Network(f"0.0.0.0/{value}")
    except ValueError as exc:
        raise ToolkitError(f"netmask is not contiguous: {value}") from exc
    # IPv4Network also accepts inverse hostmasks, which the device does not.
    if str(network.netmask) != value or network.prefixlen == 0:
        raise ToolkitError(
            f"netmask must be a nonzero contiguous subnet mask: {value}"
        )
    return value


def validate_static_network(
    address: str, netmask: str, gateway: str = "-"
) -> tuple[str, str, str]:
    """Validate static IPv4 settings against the firmware's host/subnet rules."""
    address = validate_ipv4(address, "address")
    netmask = validate_netmask(netmask)
    gateway = validate_ipv4(gateway, "gateway", optional=True)
    subnet = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)

    def validate_host(value: str, field: str) -> None:
        host = ipaddress.IPv4Address(value)
        first_octet = host.packed[0]
        # Match firmware ipv4_unicast(..., false), rather than excluding
        # otherwise usable public or documentation addresses via is_global.
        if first_octet in {0, 127} or first_octet >= 224 or host.is_link_local:
            raise ToolkitError(
                f"{field} must be a unicast IPv4 address outside the link-local range"
            )
        if host not in subnet:
            raise ToolkitError(f"{field} must be in the same subnet as address")
        if host in {subnet.network_address, subnet.broadcast_address}:
            raise ToolkitError(f"{field} must not be the network or broadcast address")

    validate_host(address, "address")
    if gateway != "-":
        validate_host(gateway, "gateway")
    return address, netmask, gateway


def parse_network_show(output: str) -> NetworkSnapshot:
    sections: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        label = line.split(maxsplit=1)[0]
        if label in {"network", "identity", "active", "staged"}:
            sections[label] = dict(KEY_VALUE_RE.findall(line))
            if label == "network":
                error_match = re.search(r"(?:^|\s)error=(.*)$", line)
                if error_match:
                    sections[label]["error"] = error_match.group(1).strip()
    required = {"network", "identity", "active"}
    missing = required - sections.keys()
    if missing:
        raise ToolkitError(
            "unexpected `network show` response; missing " + ", ".join(sorted(missing))
        )
    return NetworkSnapshot(
        runtime=sections["network"],
        identity=sections["identity"],
        active=sections["active"],
        staged=sections.get("staged"),
        raw=output,
    )


class NetworkProvisioner:
    def __init__(self, console: DiagnosticConsole) -> None:
        self.console = console

    @staticmethod
    def _ensure_ok(output: str, operation: str) -> str:
        if "result=ESP_OK" not in output:
            raise ToolkitError(f"{operation} failed: {output}")
        return output

    def show(self) -> NetworkSnapshot:
        return parse_network_show(self.console.run("network show").output)

    def stage_dhcp(self, hostname: str | None = None) -> str:
        command = "network dhcp"
        if hostname:
            command += " " + validate_hostname(hostname)
        return self._ensure_ok(
            self.console.run(command).output, "stage DHCP configuration"
        )

    def stage_static(
        self,
        address: str,
        netmask: str,
        gateway: str = "-",
        dns_primary: str = "-",
        dns_secondary: str = "-",
    ) -> str:
        address, netmask, gateway = validate_static_network(address, netmask, gateway)
        dns_primary = validate_dns(dns_primary, "primary DNS")
        dns_secondary = validate_dns(dns_secondary, "secondary DNS")
        command = (
            f"network static {address} {netmask} {gateway} "
            f"{dns_primary} {dns_secondary}"
        )
        return self._ensure_ok(
            self.console.run(command).output, "stage static configuration"
        )

    def stage_hostname(self, hostname: str) -> str:
        command = "network hostname " + validate_hostname(hostname)
        return self._ensure_ok(
            self.console.run(command).output, "stage hostname"
        )

    def apply(self) -> str:
        return self._ensure_ok(
            self.console.run("network apply").output, "apply network configuration"
        )

    def commit(self) -> str:
        return self._ensure_ok(
            self.console.run("network commit").output, "commit network configuration"
        )

    def rollback(self) -> str:
        return self._ensure_ok(
            self.console.run("network rollback").output, "rollback network configuration"
        )

    def reset(self) -> str:
        return self._ensure_ok(
            self.console.run("network reset CONFIRM").output,
            "reset network configuration",
        )
