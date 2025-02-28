from __future__ import annotations

from typing import Any

from aiohomekit.controller.ip.discovery import IpDiscovery
from aiohomekit.controller.ip.pairing import IpPairing
from aiohomekit.zeroconf import HAP_TYPE_TCP, ZeroconfController


class IpController(ZeroconfController):

    hap_type = HAP_TYPE_TCP
    discoveries: dict[str, IpDiscovery]
    pairings: dict[str, IpPairing]

    def _make_discovery(self, discovery) -> IpDiscovery:
        return IpDiscovery(self, discovery)

    def load_pairing(
        self, alias: str, pairing_data: dict[str, Any]
    ) -> IpPairing | None:
        if pairing_data["Connection"] != "IP":
            return None

        if not (hkid := pairing_data.get("AccessoryPairingID")):
            return None

        pairing = self.pairings[hkid.lower()] = IpPairing(self, pairing_data)
        self.aliases[alias] = pairing

        return pairing
