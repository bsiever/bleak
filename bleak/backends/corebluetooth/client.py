"""
BLE Client for CoreBluetooth on macOS

Created on 2019-6-26 by kevincar <kevincarrolldavis@gmail.com>
"""

import logging
import uuid
from asyncio.events import AbstractEventLoop
from typing import Callable, Any, Union
from asyncio import Future
from asyncio import coroutine

from Foundation import NSData, CBUUID
from CoreBluetooth import CBCharacteristicWriteWithResponse, CBCharacteristicWriteWithoutResponse
from functools import partial

from bleak.backends.client import BaseBleakClient
from bleak.backends.corebluetooth import CBAPP as cbapp
from bleak.backends.corebluetooth.characteristic import (
    BleakGATTCharacteristicCoreBluetooth
)
from bleak.backends.corebluetooth.descriptor import BleakGATTDescriptorCoreBluetooth
from bleak.backends.corebluetooth.discovery import discover
from bleak.backends.corebluetooth.service import BleakGATTServiceCoreBluetooth
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakError

logger = logging.getLogger(__name__)


class BleakClientCoreBluetooth(BaseBleakClient):
    """CoreBluetooth class interface for BleakClient

    Args:
        address (str): The uuid of the BLE peripheral to connect to.
        loop (asyncio.events.AbstractEventLoop): The event loop to use.

    Keyword Args:
        timeout (float): Timeout for required ``discover`` call during connect. Defaults to 2.0.

    """

    def __init__(self, address: str, loop: AbstractEventLoop = None, **kwargs):
        super(BleakClientCoreBluetooth, self).__init__(address, loop, **kwargs)

        self._device_info = None
        self._requester = None
        self._callbacks = {}
        self._services = None

    # TODO: Check this!!!
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        print("Exiting from Bleak CB")
        super(BleakClientCoreBluetooth, self).__aexit__(exc_type, exc_val, exc_tb)
        # Remove any disconnect callback (avoid memory leak)
        cbapp.central_manager_delegate._disconnected_callback[self.address] = None


    def __str__(self):
        return "BleakClientCoreBluetooth ({})".format(self.address)

    async def connect(self, **kwargs) -> bool:
        """Connect to a specified Peripheral

        Keyword Args:
            timeout (float): Timeout for required ``discover`` call. Defaults to 2.0.

        Returns:
            Boolean representing connection status.

        """
        timeout = kwargs.get("timeout", self._timeout)
        devices = await discover(timeout=timeout, loop=self.loop)
        sought_device = list(
            filter(lambda x: x.address.upper() == self.address.upper(), devices)
        )

        if len(sought_device):
            self._device_info = sought_device[0].details
        else:
            raise BleakError(
                "Device with address {} was not found".format(self.address)
            )

        logger.debug("Connecting to BLE device @ {}".format(self.address))

        await cbapp.central_manager_delegate.connect_(sought_device[0].details)

        # Now get services
        await self.get_services()

        return True

    async def disconnect(self) -> bool:
        """Disconnect from the peripheral device"""
        await cbapp.central_manager_delegate.disconnect()
        return True

    async def is_connected(self) -> bool:
        """Checks for current active connection"""
        return cbapp.central_manager_delegate.isConnected

    def set_disconnected_callback(
        self, callback: Callable[[BaseBleakClient, Future], None], **kwargs) -> None:
        """Set the disconnected callback.

        The callback will be called on DBus PropChanged event with
        the 'Connected' key set to False.

        A disconnect callback must accept two positional arguments,
        the BleakClient and the Future that called it.

        Example:

        .. code-block::python

            async with BleakClient(mac_addr, loop=loop) as client:
                def disconnect_callback(client, future):
                    print(f"Disconnected callback called on {client}!")

                client.set_disconnected_callback(disconnect_callback)

        Args:
            callback: callback to be called on disconnection.

        """

        # TODO: promote to "was disconnected" type method 
        # that does the callback (always called from CBManager)

        def curried_callback():
            f = self.loop.create_future()  # Mimic BlueZ approach
            f.set_result(None)
            _ = self.loop.create_task(coroutine(partial(callback, self, f))())
        # Configure the callback in the CBCentralManagerDelegate (which gets disconnections)
        cbapp.central_manager_delegate._disconnected_callback[self.address] = curried_callback if callback != None else None

    async def get_services(self) -> BleakGATTServiceCollection:
        """Get all services registered for this GATT server.

        Returns:
           A :py:class:`bleak.backends.service.BleakGATTServiceCollection` with this device's services tree.

        """
        if self._services is not None:
            return self._services

        logger.debug("Retrieving services...")
        services = (
            await cbapp.central_manager_delegate.connected_peripheral_delegate.discoverServices()
        )

        for service in services:
            serviceUUID = service.UUID().UUIDString()
            logger.debug(
                "Retrieving characteristics for service {}".format(serviceUUID)
            )
            characteristics = await cbapp.central_manager_delegate.connected_peripheral_delegate.discoverCharacteristics_(
                service
            )

            self.services.add_service(BleakGATTServiceCoreBluetooth(service))

            for characteristic in characteristics:
                cUUID = characteristic.UUID().UUIDString()
                logger.debug(
                    "Retrieving descriptors for characteristic {}".format(cUUID)
                )
                descriptors = await cbapp.central_manager_delegate.connected_peripheral_delegate.discoverDescriptors_(
                    characteristic
                )
                print("***********adding**************")  # BSIEVER
                self.services.add_characteristic(
                    BleakGATTCharacteristicCoreBluetooth(characteristic)
                )
                for descriptor in descriptors:
                    self.services.add_descriptor(
                        BleakGATTDescriptorCoreBluetooth(
                            descriptor, characteristic.UUID().UUIDString()
                        )
                    )
        self._services_resolved = True
        self._services = services
        return self.services

    async def read_gatt_char(self, _uuid: Union[str, uuid.UUID], use_cached=False, **kwargs) -> bytearray:
        """Perform read operation on the specified GATT characteristic.

        Args:
            _uuid (str or UUID): The uuid of the characteristics to read from.
            use_cached (bool): `False` forces macOS to read the value from the
                device again and not use its own cached value. Defaults to `False`.

        Returns:
            (bytearray) The read data.

        """
        _uuid = await self.get_appropriate_uuid(str(_uuid))
        characteristic = self.services.get_characteristic(str(_uuid))
        if not characteristic:
            raise BleakError("Characteristic {} was not found!".format(_uuid))

        output = await cbapp.central_manager_delegate.connected_peripheral_delegate.readCharacteristic_(
            characteristic.obj, use_cached=use_cached
        )
        value = bytearray(output)
        logger.debug("Read Characteristic {0} : {1}".format(_uuid, value))
        return value

    async def read_gatt_descriptor(
        self, handle: int, use_cached=False, **kwargs
    ) -> bytearray:
        """Perform read operation on the specified GATT descriptor.

        Args:
            handle (int): The handle of the descriptor to read from.
            use_cached (bool): `False` forces Windows to read the value from the
                device again and not use its own cached value. Defaults to `False`.

        Returns:
            (bytearray) The read data.
        """
        descriptor = self.services.get_descriptor(handle)
        if not descriptor:
            raise BleakError("Descriptor {} was not found!".format(handle))

        output = await cbapp.central_manager_delegate.connected_peripheral_delegate.readDescriptor_(
            descriptor.obj, use_cached=use_cached
        )
        if isinstance(
            output, str
        ):  # Sometimes a `pyobjc_unicode`or `__NSCFString` is returned and they can be used as regular Python strings.
            value = bytearray(output.encode("utf-8"))
        else:  # _NSInlineData
            value = bytearray(output)  # value.getBytes_length_(None, len(value))
        logger.debug("Read Descriptor {0} : {1}".format(handle, value))
        return value

    async def write_gatt_char(
        self, _uuid: Union[str, uuid.UUID], data: bytearray, response: bool = False
    ) -> None:
        """Perform a write operation of the specified GATT characteristic.

        Args:
            _uuid (str or UUID): The uuid of the characteristics to write to.
            data (bytes or bytearray): The data to send.
            response (bool): If write-with-response operation should be done. Defaults to `False`.

        """
        _uuid = await self.get_appropriate_uuid(str(_uuid))
        characteristic = self.services.get_characteristic(str(_uuid))
        if not characteristic:
            raise BleakError("Characteristic {} was not found!".format(_uuid))

        value = NSData.alloc().initWithBytes_length_(data, len(data))
        success = await cbapp.central_manager_delegate.connected_peripheral_delegate.writeCharacteristic_value_type_(
            characteristic.obj,
            value,
            CBCharacteristicWriteWithResponse if response else CBCharacteristicWriteWithoutResponse
        )
        if success:
            logger.debug("Write Characteristic {0} : {1}".format(_uuid, data))
        else:
            raise BleakError(
                "Could not write value {0} to characteristic {1}: {2}".format(
                    data, characteristic.uuid, success
                )
            )

    async def write_gatt_descriptor(self, handle: int, data: bytearray) -> None:
        """Perform a write operation on the specified GATT descriptor.

        Args:
            handle (int): The handle of the descriptor to read from.
            data (bytes or bytearray): The data to send.

        """
        descriptor = self.services.get_descriptor(handle)
        if not descriptor:
            raise BleakError("Descriptor {} was not found!".format(handle))

        value = NSData.alloc().initWithBytes_length_(data, len(data))
        success = await cbapp.central_manager_delegate.connected_peripheral_delegate.writeDescriptor_value_(
            descriptor.obj, value
        )
        if success:
            logger.debug("Write Descriptor {0} : {1}".format(handle, data))
        else:
            raise BleakError(
                "Could not write value {0} to descriptor {1}: {2}".format(
                    data, descriptor.uuid, success
                )
            )

    async def start_notify(
        self, _uuid: Union[str, uuid.UUID], callback: Callable[[str, Any], Any], **kwargs
    ) -> None:
        """Activate notifications/indications on a characteristic.

        Callbacks must accept two inputs. The first will be a uuid string
        object and the second will be a bytearray.

        .. code-block:: python

            def callback(sender, data):
                print(f"{sender}: {data}")
            client.start_notify(char_uuid, callback)

        Args:
            _uuid (str or UUID): The uuid of the characteristics to start notification/indication on.
            callback (function): The function to be called on notification.

        """
        _uuid = await self.get_appropriate_uuid(str(_uuid))
        characteristic = self.services.get_characteristic(str(_uuid))
        if not characteristic:
            raise BleakError("Characteristic {0} not found!".format(_uuid))

        success = await cbapp.central_manager_delegate.connected_peripheral_delegate.startNotify_cb_(
            characteristic.obj, callback
        )
        if not success:
            raise BleakError(
                "Could not start notify on {0}: {1}".format(
                    characteristic.uuid, success
                )
            )

    async def stop_notify(self, _uuid: Union[str, uuid.UUID]) -> None:
        """Deactivate notification/indication on a specified characteristic.

        Args:
            _uuid: The characteristic to stop notifying/indicating on.

        """
        _uuid = await self.get_appropriate_uuid(str(_uuid))
        characteristic = self.services.get_characteristic(str(_uuid))
        if not characteristic:
            raise BleakError("Characteristic {} not found!".format(_uuid))

        success = await cbapp.central_manager_delegate.connected_peripheral_delegate.stopNotify_(
            characteristic.obj
        )
        if not success:
            raise BleakError(
                "Could not stop notify on {0}: {1}".format(characteristic.uuid, success)
            )

    async def get_appropriate_uuid(self, _uuid: str) -> str:
        if len(_uuid) == 4:
            return _uuid.upper()

        if await self.is_uuid_16bit_compatible(_uuid):
            return _uuid[4:8].upper()

        return _uuid.upper()

    async def is_uuid_16bit_compatible(self, _uuid: str) -> bool:
        test_uuid = "0000FFFF-0000-1000-8000-00805F9B34FB"
        test_int = await self.convert_uuid_to_int(test_uuid)
        uuid_int = await self.convert_uuid_to_int(_uuid)
        result_int = uuid_int & test_int
        return uuid_int == result_int

    async def convert_uuid_to_int(self, _uuid: str) -> int:
        UUID_cb = CBUUID.alloc().initWithString_(_uuid)
        UUID_data = UUID_cb.data()
        UUID_bytes = UUID_data.getBytes_length_(None, len(UUID_data))
        UUID_int = int.from_bytes(UUID_bytes, byteorder="big")
        return UUID_int

    async def convert_int_to_uuid(self, i: int) -> str:
        UUID_bytes = i.to_bytes(length=16, byteorder="big")
        UUID_data = NSData.alloc().initWithBytes_length_(UUID_bytes, len(UUID_bytes))
        UUID_cb = CBUUID.alloc().initWithData_(UUID_data)
        return UUID_cb.UUIDString()
