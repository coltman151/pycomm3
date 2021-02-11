# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Ian Ottoway <ian@ottoway.dev>
# Copyright (c) 2014 Agostino Ruscito <ruscito@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

__all__ = ['CIPDriver', 'with_forward_open', 'parse_connection_path', ]

import ipaddress
import logging
import socket
from functools import wraps
from itertools import cycle
from os import urandom
from typing import Union, Optional, Tuple, List, Sequence, Type, Any

from .cip import (ConnectionManagerInstances, ClassCode, CIPSegment,
                  MSG_ROUTER_PATH, ConnectionManagerServices, Services,
                  PortSegment, PADDED_EPATH, DataType, UDINT, UINT)
from .const import PRIORITY, TIMEOUT_MULTIPLIER, TIMEOUT_TICKS, TRANSPORT_CLASS
from .custom_types import ModuleIdentityObject
from .exceptions import ResponseError, CommError, RequestError
from .packets import RequestPacket, ResponsePacket, RequestTypes, PacketLazyFormatter
from .socket_ import Socket
from .tag import Tag


def with_forward_open(func):
    """Decorator to ensure a forward open request has been completed with the plc"""

    @wraps(func)
    def wrapped(self, *args, **kwargs):
        opened = False
        if not self._forward_open():
            if self._cfg['extended forward open']:
                logger = logging.getLogger('pycomm3.cip_driver')
                logger.info('Extended Forward Open failed, attempting standard Forward Open.')
                self._cfg['extended forward open'] = False
                if self._forward_open():
                    opened = True
        else:
            opened = True

        if not opened:
            msg = f'Target did not connected. {func.__name__} will not be executed.'
            raise ResponseError(msg)
        return func(self, *args, **kwargs)

    return wrapped


class CIPDriver:
    """
    A base CIP driver for the SLCDriver and LogixDriver classes.  Implements common CIP services like
    (un)registering sessions, forward open/close, generic messaging, etc.
    """
    __log = logging.getLogger(f'{__module__}.{__qualname__}')

    def __init__(self, path: str, *args, large_packets: bool = True, **kwargs):
        """
        :param path: CIP path to intended target

            The path may contain 3 forms:

            - IP Address Only (``10.20.30.100``) - Use for a ControlLogix PLC is in slot 0 or if connecting to a CompactLogix or Micro800 PLC.
            - IP Address/Slot (``10.20.30.100/1``) - (ControlLogix) if PLC is not in slot 0
            - CIP Routing Path (``1.2.3.4/backplane/2/enet/6.7.8.9/backplane/0``) - Use for more complex routing.

            .. note::

                Both the IP Address and IP Address/Slot options are shortcuts, they will be replaced with the
                CIP path automatically.  The ``enet`` / ``backplane`` (or ``bp``) segments are symbols for the CIP routing
                port numbers and will be replaced with the correct value.

        :param large_packets: if True (default), the *Extended Forward Open* service will be used

            .. note::

                *Extended Forward Open* allows the used of 4KBs of service data in each request.
                The standard *Forward Open* is limited to 500 bytes.  Not all hardware supports the large packet size,
                like ENET or ENBT modules or ControlLogix version 19 or lower.  **This argument is no longer required
                as of 0.5.1, since it will automatically try a standard Forward Open if the extended one fails**
        """
        self.VERBOSE_DEBUG = False
        self._sequence = cycle(range(1, 65535))
        self._sock = kwargs.get('socket', None)
        self._session = kwargs.get('session', None)
        self._connection_opened = False
        self._target_cid = None
        self._target_is_connected = False
        self._info = {}
        ip, _path = parse_connection_path(path)

        self._cfg = {
            'context': b'_pycomm_',
            'protocol version': b'\x01\x00',
            'rpi': 5000,
            'port': 44818,
            'timeout': 10,
            'ip address': ip,
            # is cip_path the right term?  or something else?
            'cip_path': _path,
            'option': 0,
            'cid': b'\x27\x04\x19\x71',
            'csn': b'\x27\x04',
            'vid': b'\x09\x10',
            'vsn': b'\x09\x10\x19\x71',
            'extended forward open': large_packets}

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.close()
        except CommError:
            self.__log.exception('Error closing connection.')
            return False
        else:
            if not exc_type:
                return True
            self.__log.exception('Unhandled Client Error', exc_info=(exc_type, exc_val, exc_tb))
            return False

    def __repr__(self):
        _ = self._info
        return f"Program Name: {_.get('name')}, Device: {_.get('device_type', 'None')}, Revision: {_.get('revision', 'None')}"

    @property
    def connected(self) -> bool:
        """
        Read-Only Property to check whether or not a connection is open.

        :return: True if a connection is open, False otherwise
        """
        return self._connection_opened

    @property
    def connection_size(self):
        """CIP connection size, ``4000`` if using Extended Forward Open else ``500``"""
        return 4000 if self._cfg['extended forward open'] else 500

    @classmethod
    def list_identity(cls, path) -> Optional[str]:
        """
        Uses the ListIdentity service to identify the target

        :return: device identity if reply contains valid response else None
        """
        plc = cls(path)
        plc.open()
        identity = plc._list_identity()
        plc.close()
        return identity

    @classmethod
    def discover(cls):
        ip_addrs = [
            sockaddr[0]
            for family, _, _, _, sockaddr in
            socket.getaddrinfo(socket.gethostname(), None)
            if family == socket.AddressFamily.AF_INET
        ]

        driver = CIPDriver('0.0.0.0')  # dumby driver for creating the list_identity request
        driver._session = 0
        context = driver._cfg['context']
        option = driver._cfg['option']
        request = RequestTypes.list_identity()
        message = request.build_request(None, driver._session, context, option)
        devices = []

        for ip in ip_addrs:
            devices += cls._broadcast_discover(ip, message, context, request)

        if not devices:
            devices += cls._broadcast_discover(None, message, context, request)

        return devices

    @staticmethod
    def _broadcast_discover(ip, message, context, request):
        devices = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if ip:
            sock.bind((ip, 0))

        sock.sendto(message, ('255.255.255.255', 44818))

        while True:
            try:
                resp = sock.recv(4096)
                response = request.response_class(request, resp)
                if response and response.raw[12:20] == context:
                    devices.append(response.identity)
            except Exception:
                break

        return devices

    def _list_identity(self):
        request = RequestTypes.list_identity()
        response = self.send(request)
        return response.identity

    def get_module_info(self, slot: int) -> dict:
        """
        Get the Identity object for a given slot in the rack of the current connection
        """
        try:
            response = self.generic_message(
                service=Services.get_attributes_all,
                class_code=ClassCode.identity_object, instance=b'\x01',
                connected=False, unconnected_send=True,
                route_path=PADDED_EPATH.encode((PortSegment('bp', slot), ), length=True, pad_length=True),
            )

            if response:
                return ModuleIdentityObject.decode(response.value)
            else:
                raise ResponseError(f'generic_message did not return valid data - {response.error}')

        except Exception as err:
            raise ResponseError('error getting module info') from err

    def open(self):
        """
        Creates a new Ethernet/IP socket connection to target device and registers a CIP session.

        :return: True if successful, False otherwise
        """
        # handle the socket layer
        if self._connection_opened:
            return
        try:
            if self._sock is None:
                self._sock = Socket()
            self._sock.connect(self._cfg['ip address'], self._cfg['port'])
            self._connection_opened = True
            self._cfg['cid'] = urandom(4)
            self._cfg['vsn'] = urandom(4)
            if self._register_session() is None:
                self.__log.warning("Session not registered")
                return False
            return True
        except Exception as err:
            raise CommError('failed to open a connection') from err

    def _register_session(self) -> Optional[int]:
        """
        Registers a new CIP session with the target.

        :return: the session id if session registered successfully, else None
        """
        if self._session:
            return self._session

        self._session = 0
        request = RequestTypes.register_session(self._cfg['protocol version'])

        response = self.send(request)
        if response:
            self._session = response.session
            self.__log.info(f"Session = {response.session} has been registered.")
            return self._session

        self.__log.warning('Session has not been registered.')
        return None

    def _forward_open(self):
        """
        Opens a new connection with the target PLC using the *Forward Open* or *Extended Forward Open* service.

        :return: True if connection is open or was successfully opened, False otherwise
        """

        if self._target_is_connected:
            return True

        if self._session == 0:
            raise CommError("A Session Not Registered Before forward_open.")

        init_net_params = 0b_0100_0010_0000_0000  # CIP Vol 1 - 3-5.5.1.1

        if self._cfg['extended forward open']:
            net_params = UDINT.encode((self.connection_size & 0xFFFF) | init_net_params << 16)
        else:
            net_params = UINT.encode((self.connection_size & 0x01FF) | init_net_params)

        route_path = PADDED_EPATH.encode(self._cfg['cip_path'] + MSG_ROUTER_PATH, length=True)
        service = (ConnectionManagerServices.forward_open
                   if not self._cfg['extended forward open']
                   else ConnectionManagerServices.large_forward_open)

        forward_open_msg = [
            PRIORITY,
            TIMEOUT_TICKS,
            b'\x00\x00\x00\x00',  # O->T produced connection ID, not needed for us so leave blank
            self._cfg['cid'],
            self._cfg['csn'],
            self._cfg['vid'],
            self._cfg['vsn'],
            TIMEOUT_MULTIPLIER,
            b'\x00\x00\x00',  # reserved
            b'\x01\x40\x20\x00',  # O->T RPI in microseconds, RPIs are not important for us so fixed value is fine
            net_params,
            b'\x01\x40\x20\x00',  # T->O RPI
            net_params,
            TRANSPORT_CLASS,
        ]

        response = self.generic_message(
            service=service,
            class_code=ClassCode.connection_manager,
            instance=ConnectionManagerInstances.open_request,
            request_data=b''.join(forward_open_msg),
            route_path=route_path,
            connected=False,
            name='__FORWARD_OPEN__'
        )

        if response:
            self._target_cid = response.value[:4]
            self._target_is_connected = True
            self.__log.info(
                f"{'Extended ' if self._cfg['extended forward open'] else ''}Forward Open succeeded. Target CID={self._target_cid}")
            return True
        self.__log.warning(f"forward_open failed - {response.error}")
        return False

    def close(self):
        """
        Closes the current connection and un-registers the session.
        """
        errs = []
        try:
            if self._target_is_connected:
                self._forward_close()
            if self._session != 0:
                self._un_register_session()
        except Exception as err:
            errs.append(err)
            self.__log.warning(f"Error on close() -> session Err: {err}")

        try:
            if self._sock:
                self._sock.close()
        except Exception as err:
            errs.append(err)
            self.__log.warning(f"close() -> _sock.close Err: {err}")

        self._sock = None
        self._target_is_connected = False
        self._session = 0
        self._connection_opened = False

        if errs:
            raise CommError(' - '.join(str(e) for e in errs))

    def _un_register_session(self):
        """
        Un-registers the current session with the target.
        """
        request = RequestTypes.unregister_session()
        self.send(request)
        self._session = None
        self.__log.info('Session Unregistered')

    def _forward_close(self):
        """ CIP implementation of the forward close message

        Each connection opened with the forward open message need to be closed.
        Refer to ODVA documentation Volume 1 3-5.5.3

        :return: False if any error in the replayed message
        """

        if self._session == 0:
            raise CommError("A session need to be registered before to call forward_close.")

        route_path = PADDED_EPATH.encode(self._cfg['cip_path'] + MSG_ROUTER_PATH, length=True, pad_length=True)

        forward_close_msg = [
            PRIORITY,
            TIMEOUT_TICKS,
            self._cfg['csn'],
            self._cfg['vid'],
            self._cfg['vsn'],
        ]

        response = self.generic_message(
            service=ConnectionManagerServices.forward_close,
            class_code=ClassCode.connection_manager,
            instance=ConnectionManagerInstances.open_request,
            connected=False,
            route_path=route_path,
            request_data=b''.join(forward_close_msg),
            name='__FORWARD_CLOSE__'
        )
        if response:
            self._target_is_connected = False
            self.__log.info('Forward Close succeeded.')
            return True

        self.__log.warning(f"forward_close failed - {response.error}")
        return False

    def generic_message(self,
                        service: Union[int, bytes],
                        class_code: Union[int, bytes],
                        instance: Union[int, bytes],
                        attribute: Union[int, bytes] = b'',
                        request_data: Any = b'',
                        data_type: Optional[Union[Type[DataType], DataType]] = None,
                        name: str = 'generic',
                        connected: bool = True,
                        unconnected_send: bool = False,
                        route_path: Union[bool, Sequence[CIPSegment], bytes] = True) -> Tag:
        """
        Perform a generic CIP message.  Similar to how MSG instructions work in Logix.

        :param service: service code for the request (single byte)
        :param class_code: request object class ID
        :param instance: instance ID of the class
        :param attribute: (optional) attribute ID for the service/class/instance
        :param request_data: (optional) any additional data required for the request.
                                        Bytes or a value to be encoded by ``data_type``
        :param data_type: a ``DataType`` class that will be used to decode the response or encode the ``request_data``
        :param name:  return ``Tag.tag`` value, arbitrary but can be used for tracking returned Tags
        :param connected: ``True`` if service required a CIP connection (forward open), ``False`` to use UCMM
        :param unconnected_send: (Unconnected Only) wrap service in an UnconnectedSend service
        :param route_path: (Unconnected Only) ``True`` to use current connection route to destination, ``False`` to ignore,
                           Or provide list of segments to be encoded as a PADDED_EPATH.
        :return: a Tag with the result of the request. (Tag.value for writes will be the request_data)
        """

        if connected:
            with_forward_open(lambda _: None)(self)

        _kwargs = {
            'service': service,
            'class_code': class_code,
            'instance': instance,
            'attribute': attribute,
            'request_data': request_data,
            'data_type': data_type,
        }

        if not connected:
            if route_path is True:
                _kwargs['route_path'] = PADDED_EPATH.encode(self._cfg['cip_path'], length=True, pad_length=True)
            elif isinstance(route_path, bytes):
                _kwargs['route_path'] = route_path
            elif route_path:
                _kwargs['route_path'] = PADDED_EPATH.encode(route_path, length=True, pad_length=True)

            _kwargs['unconnected_send'] = unconnected_send

        req_class = RequestTypes.generic_connected if connected else RequestTypes.generic_unconnected
        request = req_class(**_kwargs)

        response = self.send(request)

        return Tag(name, response.value, None, error=response.error)

    def send(self, request: RequestPacket) -> ResponsePacket:
        if not request.error:
            request_kwargs = {
                'target_cid': self._target_cid,
                'session_id': self._session,
                'context': self._cfg['context'],
                'option': self._cfg['option'],
                'sequence': self._sequence
            }

            self._send(request.build_request(**request_kwargs))
            self.__log.debug(f'Sent: %r', request)
            reply = None if request.no_response else self._receive()
        else:
            reply = None

        response = request.response_class(request, reply)
        self.__log.debug(f'Received: %r', response)
        return response

    def _send(self, message):
        try:
            self.__log.verbose('>>> SEND >>> \n%s', PacketLazyFormatter(message))
            self._sock.send(message)
        except Exception as err:
            raise CommError('failed to send message') from err

    def _receive(self):
        try:
            reply = self._sock.receive()
        except Exception as err:
            raise CommError('failed to receive reply') from err
        else:
            self.__log.verbose('<<< RECEIVE <<< \n%s', PacketLazyFormatter(reply))
            return reply


def parse_connection_path(path: str) -> Tuple[str, List[PortSegment]]:
    """
    Parses and validates the CIP path into the destination IP and
    sequence of port/link segments.
    Returns the IP and a list of PortSegments
    """
    try:
        path = path.replace('\\', '/')
        ip, *segments = path.split('/')
        try:
            ipaddress.ip_address(ip)
        except ValueError as err:
            raise RequestError(f'Invalid IP Address: {ip}') from err

        if not segments:
            _path = [PortSegment('bp', 0), ]
        elif len(segments) == 1:
            _path = [PortSegment('bp', segments[0])]
        else:
            pairs = (segments[i:i + 2] for i in range(0, len(segments), 2))
            _path = [PortSegment(port, link) for port, link in pairs]
    except Exception as err:
        raise RequestError(f'Failed to parse connection path: {path}') from err
    else:
        return ip, _path
