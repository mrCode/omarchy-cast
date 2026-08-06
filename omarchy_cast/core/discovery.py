import ipaddress
import logging
import threading

from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

from omarchy_cast.core.device import Device

AIRPLAY_TYPE = "_airplay._tcp.local."
CAST_TYPE = "_googlecast._tcp.local."

log = logging.getLogger(__name__)


def _text(properties: dict, key: bytes) -> str | None:
    value = properties.get(key)
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _first_ipv4(info) -> str | None:
    """Devices advertise link-local IPv6 alongside IPv4; we need a routable v4."""
    for address in info.parsed_addresses():
        try:
            if isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address):
                return address
        except ValueError:
            continue
    return None


def _short_name(service_name: str) -> str:
    return service_name.split(".")[0]


def device_from_airplay(info) -> Device | None:
    address = _first_ipv4(info)
    if address is None:
        return None
    name = _short_name(info.name)
    unique = _text(info.properties, b"deviceid") or name
    return Device(
        id=Device.make_id("airplay", unique),
        name=name,
        address=address,
        port=info.port,
        protocol="airplay",
        model=_text(info.properties, b"model"),
    )


def device_from_cast(info) -> Device | None:
    address = _first_ipv4(info)
    if address is None:
        return None
    service_name = _short_name(info.name)
    unique = _text(info.properties, b"id") or service_name
    return Device(
        id=Device.make_id("cast", unique),
        name=_text(info.properties, b"fn") or service_name,
        address=address,
        port=info.port,
        protocol="cast",
        model=_text(info.properties, b"md"),
    )


PARSERS = {AIRPLAY_TYPE: device_from_airplay, CAST_TYPE: device_from_cast}


class Discovery:
    """Owns one Zeroconf instance and browses both service types.

    Note that mDNS is not always usable: some access points do not forward
    multicast between clients, and a device can be fully reachable while
    invisible here. Callers must also support connecting by raw address.
    """

    def __init__(self, zeroconf: Zeroconf | None = None) -> None:
        self._zeroconf = zeroconf or Zeroconf()
        self._owns_zeroconf = zeroconf is None
        self._devices: dict[str, Device] = {}
        self._lock = threading.Lock()
        self._browser: ServiceBrowser | None = None

    @property
    def zeroconf(self) -> Zeroconf:
        return self._zeroconf

    def start(self) -> None:
        self._browser = ServiceBrowser(
            self._zeroconf, list(PARSERS), handlers=[self._on_change]
        )

    def stop(self) -> None:
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
        if self._owns_zeroconf:
            self._zeroconf.close()

    def devices(self) -> list[Device]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: (d.protocol, d.name))

    def add(self, device: Device) -> None:
        """Register a device found by means other than mDNS (e.g. a raw address)."""
        with self._lock:
            self._devices[device.id] = device

    def remove(self, device_id: str) -> bool:
        """Drop a device from the live list.

        Only meaningful for manually added ones: anything mDNS found will be
        re-added the moment it announces again, which is correct.
        """
        with self._lock:
            return self._devices.pop(device_id, None) is not None

    # zeroconf calls this with `zeroconf` as a keyword argument.
    def _on_change(self, zeroconf, service_type, name, state_change) -> None:
        parser = PARSERS.get(service_type)
        if parser is None:
            return

        if state_change is ServiceStateChange.Removed:
            with self._lock:
                for key, device in list(self._devices.items()):
                    if device.name == _short_name(name):
                        del self._devices[key]
            return

        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        if info is None:
            return
        try:
            device = parser(info)
        except ValueError:
            log.debug("ignoring malformed service %s", name)
            return
        if device is not None:
            with self._lock:
                self._devices[device.id] = device
