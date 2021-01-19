"""
This module contains a high-level API to SNMP functions.

The arguments and return values of these functions have types which are
internal to ``puresnmp`` (subclasses of :py:class:`x690.Type`).

Alternatively, there is :py:mod:`puresnmp.api.pythonic` which converts
these values into pure Python types. This makes day-to-day programming a bit
easier but loses type information which may be useful in some edge-cases. In
such a case it's recommended to use :py:mod:`puresnmp.api.raw`.
"""

import asyncio
import logging
from asyncio import get_event_loop
from asyncio.events import AbstractEventLoop
from collections import OrderedDict
from ipaddress import ip_address
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Tuple,
)
from typing import Type as TType
from typing import TypeVar, cast

from typing_extensions import Protocol
from x690.types import (
    Integer,
    Null,
    ObjectIdentifier,
    OctetString,
    Sequence,
    Type,
)

import puresnmp.mpm as mpm
from puresnmp import security
from puresnmp.security import create as create_sm
from puresnmp.typevars import SocketResponse

from ..const import DEFAULT_TIMEOUT, ERRORS_STRICT, ERRORS_WARN
from ..credentials import V2C, Credentials
from ..exc import FaultySNMPImplementation, NoSuchOID, SnmpError
from ..pdu import (
    PDU,
    BulkGetRequest,
    EndOfMibView,
    GetNextRequest,
    GetRequest,
    GetResponse,
    NoSuchOIDPacket,
    PDUContent,
    SetRequest,
    Trap,
)
from ..snmp import VarBind
from ..transport import TSender, get_request_id, listen, send
from ..util import (
    BulkResult,
    get_unfinished_walk_oids,
    group_varbinds,
    tablify,
    validate_response_id,
)

PyType = Any  # TODO
TWalkResponse = AsyncGenerator[VarBind, None]
T = TypeVar("T", bound=TType[PyType])  # pylint: disable=invalid-name

_set = set

LOG = logging.getLogger(__name__)
OID = ObjectIdentifier


class TFetcher(Protocol):
    async def __call__(
        self, oids: List[str], timeout: int = DEFAULT_TIMEOUT
    ) -> List[VarBind]:  # pragma: no cover
        ...


class RawClient:
    def __init__(
        self,
        ip: str,
        credentials: Credentials,
        port: int = 161,
        sender: TSender = send,
        context_name: bytes = b"",
        engine_id: bytes = b"",
    ) -> None:
        self.ip = ip_address(ip)
        self.port = port
        self.credentials = credentials
        self.sender = sender
        self.engine_id = engine_id
        self.context_name = context_name
        self.lcd: Dict[str, Any] = {}

        async def handler(data: bytes) -> bytes:  # pragma: no cover
            return await sender(str(self.ip), port, data)

        self.transport_handler = handler
        self.mpm = mpm.create(
            self.credentials.mpm, self.transport_handler, self.lcd
        )

    async def _send(self, pdu: PDU, request_id: int, timeout: int) -> PDU:
        packet, security_model = await self.mpm.encode(
            request_id,
            self.credentials,
            self.engine_id,
            self.context_name,
            pdu,
        )

        raw_response = await self.sender(
            str(self.ip), self.port, bytes(packet), timeout=timeout
        )
        response = self.mpm.decode(raw_response, self.credentials)
        validate_response_id(request_id, response.value.request_id)
        return response

    async def get(self, oid: str, timeout: int = DEFAULT_TIMEOUT) -> Type:
        """
        Executes a simple SNMP GET request and returns a pure Python data
        structure.

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> # The line below needs to be "awaited" to get the result.
        >>> # This is not shown here to make it work with doctest
        >>> client.get("1.3.6.1.2.1.1.2.0")  # doctest: +ELLIPSIS
        <coroutine object ...>
        """
        result = await self.multiget([oid], timeout=timeout)
        if isinstance(result[0], NoSuchOIDPacket):
            raise NoSuchOID(oid)
        return result[0]

    async def multiget(
        self, oids: List[str], timeout: int = DEFAULT_TIMEOUT
    ) -> List[Type]:
        """
        Executes an SNMP GET request with multiple OIDs and returns a list of pure
        Python objects. The order of the output items is the same order as the OIDs
        given as arguments.

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> # The line below needs to be "awaited" to get the result.
        >>> # This is not shown here to make it work with doctest
        >>> client.multiget(['1.2.3.4', '1.2.3.5'])
        <coroutine object ...>
        """

        parsed_oids = [VarBind(oid, Null()) for oid in oids]

        request_id = get_request_id()
        pdu = GetRequest(PDUContent(request_id, parsed_oids))
        response = await self._send(pdu, request_id, timeout)

        output = [value for _, value in response.value.varbinds]
        if len(output) != len(oids):
            raise SnmpError(
                "Unexpected response. Expected %d varbind, "
                "but got %d!" % (len(oids), len(output))
            )
        return output

    async def getnext(
        self, oid: str, timeout: int = DEFAULT_TIMEOUT
    ) -> VarBind:
        """
        Executes a single SNMP GETNEXT request (used inside *walk*).

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> # The line below needs to be "awaited" to get the result.
        >>> # This is not shown here to make it work with doctest
        >>> client.getnext('1.2.3.4')
        <coroutine object ...>
        """
        result = await self.multigetnext([oid], timeout=timeout)
        return result[0]

    async def walk(
        self,
        oid: str,
        timeout: int = DEFAULT_TIMEOUT,
        errors: str = ERRORS_STRICT,
    ) -> TWalkResponse:
        """
        Executes a sequence of SNMP GETNEXT requests and returns a generator over
        :py:class:`~puresnmp.pdu.VarBind` instances.

        The generator stops when hitting an OID which is *not* a sub-node of the
        given start OID or at the end of the tree (whichever comes first).

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> client.walk('1.3.6.1.2.1.1')  # doctest: +ELLIPSIS
        <async_generator object ...>

        >>> async def example():
        ...     result = client.walk('1.3.6.1.2.1.3')
        ...     res = []
        ...     async for x in gen:
        ...         res.append(x)
        ...     pprint(res)
        >>> example()  # doctest: +SKIP
        [
            VarBind(
                oid=ObjectIdentifier("1.3.6.1.2.1.3.1.1.1.24.1.172.17.0.1"),
                value=24
            ),
            VarBind(
                oid=ObjectIdentifier("1.3.6.1.2.1.3.1.1.2.24.1.172.17.0.1"),
                value=b'\\x02B\\xef\\x14@\\xf5'
            ),
            VarBind(
                oid=ObjectIdentifier("1.3.6.1.2.1.3.1.1.3.24.1.172.17.0.1"),
                value=64, b'\\xac\\x11\\x00\\x01'
            )
        ]
        """

        async for row in self.multiwalk([oid], timeout=timeout, errors=errors):
            yield row

    async def multiwalk(
        self,
        oids: List[str],
        timeout: int = DEFAULT_TIMEOUT,
        fetcher: Optional[TFetcher] = None,
        errors: str = ERRORS_STRICT,
    ):
        """
        Executes a sequence of SNMP GETNEXT requests and returns a generator over
        :py:class:`~puresnmp.pdu.VarBind` instances.

        This is the same as :py:func:`~.walk` except that it is capable of
        iterating over multiple OIDs at the same time.

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> client.multiwalk(  # doctest: +ELLIPSIS
        ...     ['1.3.6.1.2.1.1', '1.3.6.1.4.1.1']
        ... )
        <async_generator object ...>
        """
        if fetcher is None:
            fetcher = self.multigetnext

        LOG.debug("Walking on %d OIDs using %s", len(oids), fetcher.__name__)

        varbinds = await fetcher(oids, timeout)
        # TODO: oids should be ObjectIdentifier instances on the "raw" API calls
        requested_oids = [OID(oid) for oid in oids]
        grouped_oids = group_varbinds(varbinds, requested_oids)
        unfinished_oids = get_unfinished_walk_oids(grouped_oids)

        if LOG.isEnabledFor(logging.DEBUG) and len(oids) > 1:
            LOG.debug(
                "%d of %d OIDs need to be continued",
                len(unfinished_oids),
                len(oids),
            )
        yielded = _set([])
        for var in sorted(grouped_oids.values()):
            for varbind in var:
                containment = [varbind.oid in _ for _ in requested_oids]
                if not any(containment) or varbind.oid in yielded:
                    LOG.debug(
                        "Unexpected device response: Returned VarBind %s "
                        "was either not contained in the requested tree or "
                        "appeared more than once. Skipping!",
                        varbind,
                    )
                    continue
                yielded.add(varbind.oid)
                yield varbind

        # As long as we have unfinished OIDs, we need to continue the walk for
        # those.
        while unfinished_oids:
            next_fetches = [_[1].value.oid for _ in unfinished_oids]
            next_fetches_str = [str(_) for _ in next_fetches]
            try:
                varbinds = await fetcher(next_fetches_str, timeout)
            except NoSuchOID:
                # Reached end of OID tree, finish iteration
                break
            except FaultySNMPImplementation as exc:
                if errors == ERRORS_WARN:
                    LOG.warning(
                        "SNMP walk aborted prematurely due to faulty SNMP "
                        "implementation on device %r! Upon running a "
                        "GetNext on OIDs %r it returned the following "
                        "error: %s",
                        self.ip,
                        next_fetches_str,
                        exc,
                    )
                    break
                raise
            grouped_oids = group_varbinds(
                varbinds, next_fetches, user_roots=requested_oids
            )
            unfinished_oids = get_unfinished_walk_oids(grouped_oids)
            if LOG.isEnabledFor(logging.DEBUG) and len(oids) > 1:
                LOG.debug(
                    "%d of %d OIDs need to be continued",
                    len(unfinished_oids),
                    len(oids),
                )
            for var in sorted(grouped_oids.values()):
                for varbind in var:
                    containment = [varbind.oid in _ for _ in requested_oids]
                    if not any(containment) or varbind.oid in yielded:
                        continue
                    yielded.add(varbind.oid)
                    yield varbind

    async def multigetnext(
        self, oids: List[str], timeout: int = DEFAULT_TIMEOUT
    ) -> List[VarBind]:
        """
        Executes a single multi-oid GETNEXT request.

        The request sends one packet to the remote host requesting the value of the
        OIDs following one or more given OIDs.

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> client.multigetnext(['1.2.3', '1.2.4'])  # doctest: +SKIP
        [
            VarBind(ObjectIdentifier("1.2.3.0"), Integer(1)),
            VarBind(ObjectIdentifier("1.2.4.0"), Integer(2))
        ]
        """

        varbinds = [VarBind(oid, Null()) for oid in oids]
        request_id = get_request_id()
        pdu = GetNextRequest(PDUContent(request_id, varbinds))
        response_object = await self._send(pdu, request_id, timeout)
        if len(response_object.value.varbinds) != len(oids):
            raise SnmpError(
                "Invalid response! Expected exactly %d varbind, "
                "but got %d" % (len(oids), len(response_object.value.varbinds))
            )

        output = []
        for oid, value in response_object.value.varbinds:
            if isinstance(value, EndOfMibView):
                break
            output.append(VarBind(oid, value))

        # Verify that the OIDs we retrieved are successors of the requested OIDs.
        for requested, retrieved in zip(oids, output):
            if not OID(requested) < retrieved.oid:
                # TODO remove when Py2 is dropped
                stringified = str(retrieved.oid)
                raise FaultySNMPImplementation(
                    "The OID %s is not a successor of %s!"
                    % (stringified, requested)
                )
        return output

    async def table(
        self, oid: str, num_base_nodes: int = 0, timeout: int = DEFAULT_TIMEOUT
    ) -> List[Dict[str, Any]]:

        """
        Fetch an SNMP table

        The resulting output will be a list of dictionaries where each dictionary
        corresponds to a row of the table.

        The index of the row will be contained in key ``'0'`` as a string
        representing an OID. This key ``'0'`` is automatically injected by
        ``puresnmp``. Table rows may or may not contain the row-index in other
        columns. This depends on the requested table.

        Each column ID is available as *string*.

        Example output (using fake data):

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> client.table("1.3.6.1.2.1.2.2")  # doctest: +SKIP
        [{'0': '1', '1': Integer(1), '2': Counter(30)},
         {'0': '2', '1': Integer(2), '2': Counter(123)}]
        """
        tmp = []
        if num_base_nodes == 0:
            parsed_oid = OID(oid)
            num_base_nodes = len(parsed_oid) + 1

        varbinds = self.walk(oid, timeout=timeout)
        async for varbind in varbinds:
            tmp.append(varbind)
        as_table = tablify(tmp, num_base_nodes=num_base_nodes)
        return as_table

    async def set(
        self,
        oid: str,
        value: T,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> T:
        """
        Executes a simple SNMP SET request. The result is returned as pure Python
        data structure. The value must be a subclass of
        :py:class:`~x690.types.Type`.

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> client.set(  # doctest: +SKIP
        ...     "1.3.6.1.2.1.1.4.0", OctetString(b'I am contact')
        ... )
        OctetString(b'I am contact')
        """

        result = await self.multiset({oid: value}, timeout=timeout)
        return result[oid.lstrip(".")]

    async def multiset(
        self,
        mappings: Dict[str, T],
        timeout: int = DEFAULT_TIMEOUT,
    ) -> Dict[str, T]:
        """
        Executes an SNMP SET request on multiple OIDs. The result is returned as
        pure Python data structure.

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> client.multiset([  # doctest: +SKIP
        ...     ('1.2.3', OctetString(b'foo')),
        ...     ('2.3.4', OctetString(b'bar'))
        ... ])
        {'1.2.3': b'foo', '2.3.4': b'bar'}
        """

        if not isinstance(self.credentials, V2C):
            raise SnmpError("Currently only SNMPv2c is supported!")

        if any([not isinstance(v, Type) for v in mappings.values()]):
            raise TypeError(
                "SNMP requires typing information. The value for a "
                '"set" request must be an instance of "Type"!'
            )

        binds = [VarBind(OID(k), v) for k, v in mappings.items()]

        pdu = SetRequest(PDUContent(get_request_id(), binds))
        response = await self._send(pdu, get_request_id(), timeout)

        output = {str(oid): value for oid, value in response.value.varbinds}
        if len(output) != len(mappings):
            raise SnmpError(
                "Unexpected response. Expected %d varbinds, "
                "but got %d!" % (len(mappings), len(output))
            )
        return output  # type: ignore

    async def bulkget(
        self,
        scalar_oids: List[str],
        repeating_oids: List[str],
        max_list_size: int = 1,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> BulkResult:
        # pylint: disable=unused-argument, too-many-locals
        """
        Runs a "bulk" get operation and returns a :py:class:`~.BulkResult`
        instance.  This contains both a mapping for the scalar variables (the
        "non-repeaters") and an OrderedDict instance containing the remaining list
        (the "repeaters").

        The OrderedDict is ordered the same way as the SNMP response (whatever the
        remote device returns).

        This operation can retrieve both single/scalar values *and* lists of values
        ("repeating values") in one single request. You can for example retrieve
        the hostname (a scalar value), the list of interfaces (a repeating value)
        and the list of physical entities (another repeating value) in one single
        request.

        Note that this behaves like a **getnext** request for scalar values! So you
        will receive the value of the OID which is *immediately following* the OID
        you specified for both scalar and repeating values!

        :param scalar_oids: contains the OIDs that should be fetched as single
            value.
        :param repeating_oids: contains the OIDs that should be fetched as list.
        :param max_list_size: defines the max length of each list.

        >>> from puresnmp import RawClient
        >>> client = RawClient("192.0.2.1", V2C("private"))
        >>> result = client.bulkget(  # doctest: +SKIP
        ...     scalar_oids=['1.3.6.1.2.1.1.1',
        ...                  '1.3.6.1.2.1.1.2'],
        ...     repeating_oids=['1.3.6.1.2.1.3.1',
        ...                     '1.3.6.1.2.1.5.1'],
        ...     max_list_size=10)
        BulkResult(
            scalars={'1.3.6.1.2.1.1.2.0': '1.3.6.1.4.1.8072.3.2.10',
                        '1.3.6.1.2.1.1.1.0': b'Linux aafa4dce0ad4 4.4.0-28-'
                                            b'generic #47-Ubuntu SMP Fri Jun 24 '
                                            b'10:09:13 UTC 2016 x86_64'},
            listing=OrderedDict([
                ('1.3.6.1.2.1.3.1.1.1.10.1.172.17.0.1', 10),
                ('1.3.6.1.2.1.5.1.0', b'\x01'),
                ('1.3.6.1.2.1.3.1.1.2.10.1.172.17.0.1', b'\x02B\x8e>\x9ee'),
                ('1.3.6.1.2.1.5.2.0', b'\x00'),
                ('1.3.6.1.2.1.3.1.1.3.10.1.172.17.0.1', b'\xac\x11\x00\x01'),
                ('1.3.6.1.2.1.5.3.0', b'\x00'),
                ('1.3.6.1.2.1.4.1.0', 1),
                ('1.3.6.1.2.1.5.4.0', b'\x01'),
                ('1.3.6.1.2.1.4.3.0', b'\x00\xb1'),
                ('1.3.6.1.2.1.5.5.0', b'\x00'),
                ('1.3.6.1.2.1.4.4.0', b'\x00'),
                ('1.3.6.1.2.1.5.6.0', b'\x00'),
                ('1.3.6.1.2.1.4.5.0', b'\x00'),
                ('1.3.6.1.2.1.5.7.0', b'\x00'),
                ('1.3.6.1.2.1.4.6.0', b'\x00'),
                ('1.3.6.1.2.1.5.8.0', b'\x00'),
                ('1.3.6.1.2.1.4.7.0', b'\x00'),
                ('1.3.6.1.2.1.5.9.0', b'\x00'),
                ('1.3.6.1.2.1.4.8.0', b'\x00'),
                ('1.3.6.1.2.1.5.10.0', b'\x00')]))
        """

        scalar_oids = scalar_oids or []  # protect against empty values
        repeating_oids = repeating_oids or []  # protect against empty values

        oids = [OID(oid) for oid in scalar_oids] + [
            OID(oid) for oid in repeating_oids
        ]

        non_repeaters = len(scalar_oids)

        request_id = get_request_id()
        pdu = BulkGetRequest(request_id, non_repeaters, max_list_size, *oids)
        get_response = await self._send(pdu, request_id, timeout)

        # See RFC=3416 for details of the following calculation
        n = min(non_repeaters, len(oids))
        m = max_list_size
        r = max(len(oids) - n, 0)  # pylint: disable=invalid-name
        expected_max_varbinds = n + (m * r)

        n_retrieved_varbinds = len(get_response.value.varbinds)
        if n_retrieved_varbinds > expected_max_varbinds:
            raise SnmpError(
                "Unexpected response. Expected no more than %d "
                "varbinds, but got %d!"
                % (expected_max_varbinds, n_retrieved_varbinds)
            )

        # cut off the scalar OIDs from the listing(s)
        scalar_tmp = get_response.value.varbinds[0 : len(scalar_oids)]
        repeating_tmp = get_response.value.varbinds[len(scalar_oids) :]

        # prepare output for scalar OIDs
        scalar_out = {str(oid): value for oid, value in scalar_tmp}

        # prepare output for listing
        repeating_out = OrderedDict()  # type: Dict[str, Type[PyType]]
        for oid, value in repeating_tmp:
            if isinstance(value, EndOfMibView):
                break
            repeating_out[str(oid)] = value

        return BulkResult(scalar_out, repeating_out)

    def _bulkwalk_fetcher(self, bulk_size: int = 10) -> TFetcher:
        """
        Create a bulk fetcher with a fixed limit on "repeatable" OIDs.
        """

        async def fetcher(
            oids: List[str],
            timeout: int = DEFAULT_TIMEOUT,
        ) -> List[VarBind]:
            """
            Executes a SNMP BulkGet request.
            """
            result = await self.bulkget(
                [], oids, max_list_size=bulk_size, timeout=timeout
            )
            return [VarBind(OID(k), v) for k, v in result.listing.items()]

        fetcher.__name__ = "_bulkwalk_fetcher(%d)" % bulk_size
        return fetcher

    async def bulkwalk(
        self,
        oids: List[str],
        bulk_size: int = 10,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> TWalkResponse:
        """
        More efficient implementation of :py:func:`~.walk`. It uses
        :py:func:`~.bulkget` under the hood instead of :py:func:`~.getnext`.

        Just like :py:func:`~.multiwalk`, it returns a generator over
        :py:class:`~puresnmp.pdu.VarBind` instances.

        :param ip: The IP address of the target host.
        :param community: The community string for the SNMP connection.
        :param oids: A list of base OIDs to use in the walk operation.
        :param bulk_size: How many varbinds to request from the remote host with
            one request.
        :param port: The TCP port of the remote host.
        :param timeout: The TCP timeout for network calls

        Example::

            >>> from puresnmp import RawClient, V2C
            >>> def example():
            ...     ip = '127.0.0.1'
            ...     community = 'private'
            ...     oids = [
            ...         '1.3.6.1.2.1.2.2.1.2',   # name
            ...         '1.3.6.1.2.1.2.2.1.6',   # MAC
            ...         '1.3.6.1.2.1.2.2.1.22',  # ?
            ...     ]
            ...     client = RawClient(ip, V2C(community))
            ...     result = client.bulkwalk(oids)
            ...     for row in result:
            ...         print(row)
            >>> example()  # doctest: +SKIP
            VarBind(oid=ObjectIdentifier("1.3.6.1.2.1.2.2.1.2.1"), value=b'lo')
            VarBind(oid=ObjectIdentifier("1.3.6.1.2.1.2.2.1.6.1"), value=b'')
            VarBind(oid=ObjectIdentifier("1.3.6.1.2.1.2.2.1.22.1"), value='0.0')
            VarBind(oid=ObjectIdentifier("1.3.6.1.2.1.2.2.1.2.38"), value=b'eth0')
            VarBind(oid=ObjectIdentifier("1.3.6.1.2.1.2.2.1.6.38"), value=b'\x02B\xac\x11\x00\x02')
            VarBind(oid=ObjectIdentifier("1.3.6.1.2.1.2.2.1.22.38"), value='0.0')
        """

        if not isinstance(oids, list):
            raise TypeError("OIDS need to be passed as list!")

        result = self.multiwalk(
            oids,
            fetcher=self._bulkwalk_fetcher(bulk_size),
            timeout=timeout,
        )
        async for oid, value in result:
            yield VarBind(oid, value)

    async def bulktable(
        self, oid: str, num_base_nodes: int = 0, bulk_size: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Fetch an SNMP table using "bulk" requests.

        See :py:func:`.table` for more information of the returned structure.

        .. versionadded: 1.7.0
        """
        tmp = []
        if num_base_nodes == 0:
            parsed_oid = OID(oid)
            num_base_nodes = len(parsed_oid) + 1

        varbinds = self.bulkwalk([oid], bulk_size=bulk_size)
        async for varbind in varbinds:
            tmp.append(varbind)
        as_table = tablify(tmp, num_base_nodes=num_base_nodes)
        return as_table


def register_trap_callback(
    callback: Callable[[PDU], Any],
    listen_address: str = "0.0.0.0",
    port: int = 162,
    credentials: Optional[Credentials] = None,
    loop: Optional[AbstractEventLoop] = None,
) -> AbstractEventLoop:
    """
    Registers a callback function for for SNMP traps.

    Every time a trap is received, the callback is called with the PDU
    contained in that trap.

    As per :rfc:`3416#section-4.2.6`, the first two varbinds are the system
    uptime and the trap OID. The following varbinds are the body of the trap

    The callback will be called on the current asyncio loop. Alternatively, a
    loop can be passed into this function in which case, the traps will be
    handler on that loop instead.
    """
    if loop is None:
        loop = get_event_loop()

    def decode(packet: SocketResponse) -> None:
        async def handler(data: bytes) -> bytes:
            return await send(str(packet.info.address), packet.info.port, data)

        lcd: Dict[str, Any] = {}

        obj = cast(
            Tuple[Integer, Integer, Trap], Sequence.decode(packet.data)[0]
        )

        mproc = mpm.create(obj[0].value, handler, lcd)
        trap = mproc.decode(packet.data, credentials)
        asyncio.ensure_future(callback(trap))

    handler = listen(listen_address, port, decode, loop)
    loop.run_until_complete(handler)
    return loop
