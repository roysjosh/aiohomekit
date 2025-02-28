#
# Copyright 2022 aiohomekit team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
import uuid

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from aiohomekit.controller.abstract import AbstractDiscovery, FinishPairing
from aiohomekit.model import CharacteristicsTypes, ServicesTypes
from aiohomekit.model.feature_flags import FeatureFlags
from aiohomekit.protocol import perform_pair_setup_part1, perform_pair_setup_part2
from aiohomekit.utils import check_pin_format, pair_with_auth

from .bleak import AIOHomeKitBleakClient
from .client import (
    char_read,
    char_write,
    drive_pairing_state_machine,
    retry_bluetooth_connection_error,
)
from .connection import establish_connection
from .manufacturer_data import HomeKitAdvertisement
from .pairing import BlePairing

if TYPE_CHECKING:
    from aiohomekit.controller.ble.controller import BleController


logger = logging.getLogger(__name__)


class BleDiscovery(AbstractDiscovery):

    """
    A discovered BLE HAP device that is unpaired.
    """

    description: HomeKitAdvertisement

    def __init__(
        self,
        controller: BleController,
        device: BLEDevice,
        description: HomeKitAdvertisement,
    ) -> None:
        self.description = description
        self.controller = controller
        self.device = device
        self.client: AIOHomeKitBleakClient | None = None
        self._connection_lock = asyncio.Lock()

    @property
    def name(self):
        return f"{self.description.name} ({self.description.address})"

    async def _ensure_connected(self):
        logger.debug("Ensure connected with device %s", self.device)
        if self.client and self.client.is_connected:
            return
        async with self._connection_lock:
            # Check again while holding the lock
            if self.client and self.client.is_connected:
                return
            self.client = await establish_connection(
                self.device,
                self.name,
                self._async_disconnected,
            )

    def _async_disconnected(self, client: AIOHomeKitBleakClient) -> None:
        logger.debug("%s: Session closed callback", self.name)

    async def _close(self):
        if not self.client:
            return
        async with self._connection_lock:
            if not self.client or not self.client.is_connected:
                return
            logger.debug("Disconnecting from %s", self.name)
            try:
                await self.client.disconnect()
            except BleakError:
                logger.debug(
                    "Failed to close connection, client may have already closed it"
                )

    async def _async_start_pairing(self, alias: str) -> tuple[bytearray, bytearray]:
        await self._ensure_connected()

        ff_char = self.client.get_characteristic(
            ServicesTypes.PAIRING,
            CharacteristicsTypes.PAIRING_FEATURES,
        )
        ff_iid = await self.client.get_characteristic_iid(ff_char)
        ff_raw = await char_read(self.client, None, None, ff_char, ff_iid)
        ff = FeatureFlags(ff_raw[0])
        return await drive_pairing_state_machine(
            self.client,
            CharacteristicsTypes.PAIR_SETUP,
            perform_pair_setup_part1(
                with_auth=pair_with_auth(ff),
            ),
        )

    @retry_bluetooth_connection_error()
    async def async_start_pairing(self, alias: str) -> FinishPairing:
        salt, pub_key = await self._async_start_pairing(alias)
        attempt = 0

        @retry_bluetooth_connection_error()
        async def finish_pairing(pin: str) -> BlePairing:
            nonlocal attempt
            nonlocal salt
            nonlocal pub_key

            check_pin_format(pin)

            attempt += 1

            if attempt > 1:
                # We've already tried to pair, if
                # the retry gets us here again, we
                # need to disconnect and restart
                # the pairing process.
                await self._close()
                salt, pub_key = await self._async_start_pairing(alias)

            pairing = await drive_pairing_state_machine(
                self.client,
                CharacteristicsTypes.PAIR_SETUP,
                perform_pair_setup_part2(
                    pin,
                    str(uuid.uuid4()),
                    salt,
                    pub_key,
                ),
            )

            pairing["AccessoryAddress"] = self.description.address
            pairing["Connection"] = "BLE"

            obj = self.controller.pairings[alias] = BlePairing(
                self.controller,
                pairing,
                client=self.client,
                description=self.description,
            )
            return obj

        return finish_pairing

    async def async_identify(self) -> None:
        if self.paired:
            raise RuntimeError(
                f"{self.name}: Cannot anonymously identify a paired accessory"
            )

        await self._ensure_connected()

        char = self.client.get_characteristic(
            ServicesTypes.ACCESSORY_INFORMATION,
            CharacteristicsTypes.IDENTIFY,
        )
        iid = await self.client.get_characteristic_iid(char)

        await char_write(self.client, None, None, char, iid, b"\x01")

    def _async_process_advertisement(
        self, device: BLEDevice, description: HomeKitAdvertisement
    ):
        """Update the device and description so we connect to the right place."""
        self.device = device
        self.description = description
