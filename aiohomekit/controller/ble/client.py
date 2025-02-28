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

import logging
import random
from typing import Any, Callable, TypeVar, cast

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from aiohomekit.controller.ble.key import DecryptionKey, EncryptionKey
from aiohomekit.exceptions import EncryptionError
from aiohomekit.model.services import ServicesTypes
from aiohomekit.pdu import (
    OpCode,
    PDUStatus,
    decode_pdu,
    decode_pdu_continuation,
    encode_pdu,
)
from aiohomekit.protocol.tlv import TLV

from .bleak import BLEAK_EXCEPTIONS, AIOHomeKitBleakClient
from .const import AdditionalParameterTypes
from .structs import BleRequest

logger = logging.getLogger(__name__)

WrapFuncType = TypeVar("WrapFuncType", bound=Callable[..., Any])

DEFAULT_ATTEMPTS = 2


def retry_bluetooth_connection_error(attempts: int = DEFAULT_ATTEMPTS) -> WrapFuncType:
    """Define a wrapper to retry on bluetooth connection error."""

    def _decorator_retry_bluetooth_connection_error(func: WrapFuncType) -> WrapFuncType:
        """Define a wrapper to retry on bleak error.

        The accessory is allowed to disconnect us any time so
        we need to retry the operation.
        """

        async def _async_wrap(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(attempts):
                try:
                    return await func(*args, **kwargs)
                except BLEAK_EXCEPTIONS:
                    if attempt == attempts - 1:
                        raise
                    logger.debug(
                        "Bleak error calling %s, retrying...", func, exc_info=True
                    )

        return cast(WrapFuncType, _async_wrap)

    return cast(WrapFuncType, _decorator_retry_bluetooth_connection_error)


async def ble_request(
    client: AIOHomeKitBleakClient,
    encryption_key: EncryptionKey | None,
    decryption_key: DecryptionKey | None,
    opcode: OpCode,
    handle: BleakGATTCharacteristic,
    iid: int,
    data: bytes | None = None,
) -> tuple[PDUStatus, bytes]:
    tid = random.randrange(1, 254)

    # We think there is a 3 byte overhead for ATT
    # https://github.com/jlusiardi/homekit_python/issues/211#issuecomment-996751939
    # But we haven't confirmed that this isn't already taken into account
    debug_enabled = logger.isEnabledFor(logging.DEBUG)

    # Newer bleak, not currently released
    if max_write_without_response_size := getattr(
        handle, "max_write_without_response_size", None
    ):
        if debug_enabled:
            logger.debug(
                "max_write_without_response_size: %s, mtu_size-3: %s",
                max_write_without_response_size,
                client.mtu_size - 3,
            )
        fragment_size = max(max_write_without_response_size, client.mtu_size - 3)
    # Bleak 0.15.1 and below
    elif (
        (char_obj := getattr(handle, "obj", None))
        and isinstance(char_obj, dict)
        and (char_mtu := char_obj.get("MTU"))
    ):
        if debug_enabled:
            logger.debug(
                "bleak obj MTU: %s, mtu_size-3: %s",
                char_mtu,
                client.mtu_size - 3,
            )
        fragment_size = max(char_mtu - 3, client.mtu_size - 3)
    else:
        if debug_enabled:
            logger.debug(
                "no bleak obj MTU or max_write_without_response_size, using mtu_size-3: %s",
                client.mtu_size - 3,
            )
        fragment_size = client.mtu_size - 3

    if encryption_key:
        # Secure session means an extra 16 bytes of overhead
        fragment_size -= 16

    if debug_enabled:
        logger.debug("Using fragment size: %s", fragment_size)

    # Wrap data in one or more PDU's split at fragment_size
    # And write each one to the target characterstic handle
    writes = []
    for data in encode_pdu(opcode, tid, iid, data, fragment_size):
        if debug_enabled:
            logger.debug("Queuing fragment for write: %s", data)
        if encryption_key:
            data = encryption_key.encrypt(data)
        writes.append(data)

    for write in writes:
        await client.write_gatt_char(handle, write, True)

    data = await client.read_gatt_char(handle)
    if decryption_key:
        data = decryption_key.decrypt(data)
        if data is False:
            raise EncryptionError("Decryption failed")

    if debug_enabled:
        logger.debug("Read fragment: %s", data)

    # Validate the PDU header
    status, expected_length, data = decode_pdu(tid, data)

    # If packet is too short then there may be 1 or more continuation
    # packets. Keep reading until we have enough data.
    #
    # Even if the status is failure, we must read the whole
    # data set or the encryption will be out of sync.
    #
    while len(data) < expected_length:
        next = await client.read_gatt_char(handle)
        if decryption_key:
            next = decryption_key.decrypt(next)
            if next is False:
                raise EncryptionError("Decryption failed")
        if debug_enabled:
            logger.debug("Read fragment: %s", next)

        data += decode_pdu_continuation(tid, next)

    return status, data


async def char_write(
    client: BleakClient,
    encryption_key: EncryptionKey | None,
    decryption_key: DecryptionKey | None,
    handle: BleakGATTCharacteristic,
    iid: int,
    body: bytes,
):
    body = BleRequest(expect_response=1, value=body).encode()
    pdu_status, data = await ble_request(
        client, encryption_key, decryption_key, OpCode.CHAR_WRITE, handle, iid, body
    )
    if pdu_status != PDUStatus.SUCCESS:
        raise ValueError(
            f"{client.address}: PDU status was not success: {pdu_status.description} ({pdu_status.value})"
        )
    decoded = dict(TLV.decode_bytes(data))
    return decoded[AdditionalParameterTypes.Value.value]


async def char_read(
    client: BleakClient,
    encryption_key: EncryptionKey | None,
    decryption_key: DecryptionKey | None,
    handle: BleakGATTCharacteristic,
    iid: int,
):
    pdu_status, data = await ble_request(
        client, encryption_key, decryption_key, OpCode.CHAR_READ, handle, iid
    )
    if pdu_status != PDUStatus.SUCCESS:
        raise ValueError(
            f"{client.address}: PDU status was not success: {pdu_status.description} ({pdu_status.value})"
        )
    decoded = dict(TLV.decode_bytes(data))
    return decoded[AdditionalParameterTypes.Value.value]


async def drive_pairing_state_machine(
    client: AIOHomeKitBleakClient,
    characteristic: str,
    state_machine: Any,
) -> Any:
    char = client.get_characteristic(ServicesTypes.PAIRING, characteristic)
    iid = await client.get_characteristic_iid(char)

    request, expected = state_machine.send(None)
    while True:
        try:
            body = TLV.encode_list(request)
            response = await char_write(
                client,
                None,
                None,
                char,
                iid,
                body,
            )
            request, expected = state_machine.send(TLV.decode_bytes(response))
        except StopIteration as result:
            return result.value
