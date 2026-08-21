from __future__ import annotations

from regscribe.converter import Log, Register
import re
import time
from queue import SimpleQueue, Empty
from threading import Lock, Event
import threading

class WriteRequestBase:
    def __init__(self, addr, value):
        self.addr = addr
        self.value = value

    def size(self):
        return len(bytes(self)) * 8

    def __bytes__(self):
        return bytes(
            bytearray(
                [
                    0x21 | ((self.addr & 0x03) << 6),
                    (self.addr >> 2) & 0xFF,
                    (self.value >> 0) & 0xFF,
                    (self.value >> 8) & 0xFF,
                    (self.value >> 16) & 0xFF,
                    (self.value >> 24) & 0xFF,
                ]
            )
        )

    # def __str__(self):
    #     return f"0x{self.addr:04X} -> 0x{self.value:08X}"

    def __str__(self):
        return f"WriteReq 0x{''.join([f'{b:02X}' for b in bytes(self)])} ({', '.join([f'{k}=0x{v:X}' for k, v in vars(self).items()])})"


class ReadRequestBase:
    def __init__(self, addr):
        self.addr = addr

    def size(self):
        return len(bytes(self)) * 8

    def __bytes__(self):
        return bytes(bytearray([0x01 | ((self.addr & 0x03) << 6), (self.addr >> 2) & 0xFF]))

    def __str__(self):
        return f"ReadReq 0x{''.join([f'{b:02X}' for b in bytes(self)])} ({', '.join([f'{k}=0x{v:X}' for k, v in vars(self).items()])})"


class ReadResponseBase:
    BYTES = 6

    def __init__(self, bytes: bytes | bytearray):
        resp = bytearray(bytes)
        self.sync = resp[0] & 0x03
        self.time = (resp[0] >> 2) & 0x0F
        self.addr = (resp[1] << 2) | ((resp[0] & 0xC0) >> 6)
        self.value = resp[5] << 24 | resp[4] << 16 | resp[3] << 8 | resp[2]

    def __str__(self):
        return f"ReadResp 0x{''.join([f'{b:02X}' for b in bytes(self)])} ({', '.join([f'{k}=0x{v:X}' for k, v in vars(self).items()])})"

    
    def size(self):
        return len(bytes(self)) * 8

    def valid(self):
        return self.sync == 0x01

    def __bytes__(self):
        return bytes(
            bytearray(
                [
                    ((self.sync & 0x03) << 6) | ((self.time & 0x0F) << 2) | ((self.addr & 0x03) << 6),
                    (self.addr >> 2) & 0xFF,
                    (self.value >> 0) & 0xFF,
                    (self.value >> 8) & 0xFF,
                    (self.value >> 16) & 0xFF,
                    (self.value >> 24) & 0xFF,
                ]
            )
        )


# spec letters and the attribute they fill
SPEC_ATTRIBUTES = {"A": "addr", "D": "value", "T": "time"}

_SPEC_TOKEN = re.compile(r"\s*(?:(?P<const>[01Xx])(?:\[(?P<count>\d+)\])?|(?P<name>[A-Za-z])(?:\[(?P<lsb>\d+):(?P<msb>\d+)\])?)")


def parse_datagram_spec(spec: str):
    """Parse a wire format like "1XA[0:9]D[0:31]" into (segments, const_mask, const_value, bits).

    Tokens are listed in wire order, LSB first: '0'/'1' is a constant bit, 'X' a don't care bit that
    is ignored on receive and sent as 0, and '0[n]'/'1[n]'/'X[n]' repeat it n times. A letter is a
    single field bit and letter[lsb:msb] is an inclusive field bit range (msb < lsb reverses it).
    Each segment is (attribute, field_lsb, field_mask, wire_offset).
    """
    segments = []
    const_mask = 0
    const_value = 0
    bit = 0
    pos = 0
    while pos < len(spec):
        token = _SPEC_TOKEN.match(spec, pos)
        if token is None:
            raise ValueError(f"Invalid datagram spec {spec!r} at position {pos}: {spec[pos:]!r}")
        pos = token.end()

        if token["const"] is not None:
            count = int(token["count"] or 1)
            if token["const"] in "01":
                mask = ((1 << count) - 1) << bit
                const_mask |= mask
                const_value |= mask if token["const"] == "1" else 0
            bit += count
            continue

        attr = SPEC_ATTRIBUTES.get(token["name"].upper(), token["name"].lower())
        if token["lsb"] is None:
            segments.append((attr, 0, 1, bit))
            bit += 1
        elif int(token["msb"]) >= int(token["lsb"]):
            lsb, msb = int(token["lsb"]), int(token["msb"])
            segments.append((attr, lsb, (1 << (msb - lsb + 1)) - 1, bit))
            bit += msb - lsb + 1
        else:
            for field_bit in range(int(token["lsb"]), int(token["msb"]) - 1, -1):
                segments.append((attr, field_bit, 1, bit))
                bit += 1

    return segments, const_mask, const_value, bit


def _datagram_class(spec: str, name: str, base: type, params: tuple[str, ...] | None, defaults: dict):
    segments, const_mask, const_value, bits = parse_datagram_spec(spec)
    nbytes = -(-bits // 8)
    if bits % 8:
        Log.warn(f"Datagram spec {spec!r} is {bits} bits, padded to {nbytes} bytes")
    segments = tuple(segments)
    fields = tuple(dict.fromkeys(attr for attr, _, _, _ in segments))
    fields_mask = 0
    for _, _, mask, offset in segments:
        fields_mask |= mask << offset
    wire_mask = ((1 << (nbytes * 8)) - 1) & ~fields_mask

    blank = dict.fromkeys(fields, 0)
    blank.update(defaults)

    def unpack_init(self, data):
        d = self.__dict__
        d.update(blank)
        word = int.from_bytes(data, "little")
        # keep the received non-field bits so a datagram prints exactly as it came in
        d["_wire"] = word & wire_mask
        for attr, lsb, mask, offset in segments:
            d[attr] |= ((word >> offset) & mask) << lsb

    def field_init(self, *values):
        d = self.__dict__
        d.update(blank)
        d.update(zip(params, values))

    class Datagram(base):
        SPEC = spec
        BYTES = nbytes
        FIELDS = fields
        CONST_MASK = const_mask
        CONST_VALUE = const_value
        # everything that is not a field: constants, don't cares and padding
        _wire = const_value

        __init__ = unpack_init if params is None else field_init

        def valid(self):
            return (self._wire & const_mask) == const_value

        def size(self):
            return nbytes * 8

        def __bytes__(self):
            word = self._wire
            d = self.__dict__
            for attr, lsb, mask, offset in segments:
                word |= ((d[attr] >> lsb) & mask) << offset
            return word.to_bytes(nbytes, "little")

        def __str__(self):
            values = ", ".join(f"{attr}=0x{self.__dict__[attr]:X}" for attr in fields)
            return f"{name} 0x{bytes(self).hex().upper()} ({values})"

    Datagram.__name__ = Datagram.__qualname__ = name
    return Datagram


def read_request_class(spec: str) -> type[ReadRequestBase]:
    return _datagram_class(spec, "ReadReq", ReadRequestBase, ("addr",), {})


def write_request_class(spec: str) -> type[WriteRequestBase]:
    return _datagram_class(spec, "WriteReq", WriteRequestBase, ("addr", "value"), {})


def read_response_class(spec: str) -> type[ReadResponseBase]:
    response = _datagram_class(spec, "ReadResp", ReadResponseBase, None, {"time": 0})
    if not response.CONST_MASK:
        # without constant bits valid() cannot reject anything, so the rx loop can never resync
        Log.warn(f"Response spec {spec!r} has no constant bits to validate against")
    return response


class comm:
    class Listener:
        def __init__(self, priority, samples=None, duration=None):
            self.priority = priority
            self.samples = samples
            self.deadline = None if duration is None else time.monotonic() + duration
            self.issued = 0
            self.done = Event()

    class Monitored:
        def __init__(self, node):
            self.node = node
            self.listener: dict[str, comm.Listener] = dict()
            self.counter = 1

        def add_listener(self, name, priority, samples=None, duration=None) -> comm.Listener:
            self.listener[name] = comm.Listener(priority, samples=samples, duration=duration)
            self.counter = min([self.counter, self.lowest_priority()])
            return self.listener[name]

        def reset_counter(self):
            self.counter = self.lowest_priority()

        def remove_listener(self, name):
            for listener in list(self.listener) if name is None else [name]:
                done = self.listener.pop(listener, None)
                if done is not None:
                    done.done.set()

        def take_sample(self):
            now = time.monotonic()
            for name, listener in list(self.listener.items()):
                listener.issued += 1
                if listener.samples is not None:
                    listener.samples -= 1
                if (listener.samples is not None and listener.samples <= 0) or (listener.deadline is not None and now >= listener.deadline):
                    self.remove_listener(name)

        def lowest_priority(self):
            return min(listener.priority for listener in self.listener.values())

        def has_listener(self):
            return bool(self.listener)

    class RegisterMonitor:
        def __init__(self, ):
            self.monitored = dict()
            self.it = iter(self.monitored.values())
            self.lock = Lock()

        def add_listener(self, node, name, prio, samples=None, duration=None) -> comm.Listener | None:
            if prio == 0:
                self.remove_listener(node, name)
                return None
            else:
                with self.lock:
                    mon = self.monitored.pop(node, comm.Monitored(node))
                    listener = mon.add_listener(name, prio, samples=samples, duration=duration)
                    self.monitored[node] = mon

                    self.it = iter(self.monitored.values())
                    return listener

        def remove_listener(self, node, name):
            with self.lock:
                mon = self.monitored.pop(node, comm.Monitored(node))
                mon.remove_listener(name)
                if mon.has_listener():
                    self.monitored[node] = mon

                self.it = iter(self.monitored.values())

        def get_next(self) -> Register | None:  # may be called in a thread...
            with self.lock:
                while True:
                    mon = next(self.it, None)
                    if mon is None:
                        self.it = iter(self.monitored.values())
                        mon = next(self.it, None)
                    if mon is None:
                        return None

                    if mon.counter <= 1:
                        mon.reset_counter()
                        mon.take_sample()
                        if not mon.has_listener():
                            self.monitored.pop(mon.node, None)
                            self.it = iter(self.monitored.values())
                        return mon.node
                    else:
                        mon.counter -= 1

    class ValueUpdate:
        def __init__(self, value, time):
            self.value = value
            self.time = time

    class ValueUpdates:
        def __init__(self):
            self.updates = dict()

        def add_update(self, node, value, time):
            if node is not None:
                update = comm.ValueUpdate(value, time)
                if node in self.updates:
                    if self.updates[node][-1].time != time:
                        self.updates[node].append(update)

                    # if len(self.updates[node]) > 100:
                    #     del self.updates[node][0]
                else:
                    self.updates[node] = [update]
            else:
                Log.error("tried to add None node to update")

        def clear(self):
            self.updates.clear()

        def to_dict(self):
            d = dict()
            for node, updates in self.updates.items():
                d[node.id] = list()
                for update in updates:
                    d[node.id].append({"value": update.value, "time": update.time})
            return d

    class RequestedValue:
        def __init__(self, request, time):
            self.addr = request.addr
            # self.node = node
            self.time = time

    class RequestedValues:
        def __init__(self):
            self.requests: SimpleQueue[comm.RequestedValue] = SimpleQueue()

        def add_request(self, request: ReadRequestBase | WriteRequestBase, t=None):
            if isinstance(request, ReadRequestBase):
                if t is None:
                    t = time.time_ns()
                self.requests.put(comm.RequestedValue(request, t))

        def received_response(self, response: ReadResponseBase):
            try:
                req = self.requests.get(block=False)
            except Empty:
                Log.warn(f"Received response for address 0x{response.addr:04X} without a pending request")
                return
            if req.addr != response.addr:
                Log.warn(f"Received response for address 0x{response.addr:04X} but expected 0x{req.addr:04X}")
            elif Log.debug_enabled():
                Log.debug(f"Matched response for address 0x{response.addr:04X}")

        def open_requests(self):
            return self.requests.qsize()

        def clear(self):
            try:
                while True:
                    self.requests.get(block=False)
            except Empty:
                pass

    def __init__(self, project, ReadReq: type | str = ReadRequestBase, WriteReq: type | str = WriteRequestBase, ReadResp: type | str = ReadResponseBase):
        self.project = project
        self.ReadRequest: type[ReadRequestBase] = read_request_class(ReadReq) if isinstance(ReadReq, str) else ReadReq
        self.WriteRequest: type[WriteRequestBase] = write_request_class(WriteReq) if isinstance(WriteReq, str) else WriteReq
        self.ReadResponse: type[ReadResponseBase] = read_response_class(ReadResp) if isinstance(ReadResp, str) else ReadResp

        self.req_queue : SimpleQueue[ReadRequestBase | WriteRequestBase] = SimpleQueue()
        self.resp_queue : SimpleQueue[ReadResponseBase | None] = SimpleQueue()
        # without a time field in the datagram every sample would carry the same timestamp
        self.resp_has_time = "time" in getattr(self.ReadResponse, "FIELDS", ("time",))

        # self.last_handle_time = time.perf_counter()
        self.updates = comm.ValueUpdates()
        self.regmon = comm.RegisterMonitor()
        self.prev_sampletime = None
        # self.read_queue = bytes()
        self.requests = comm.RequestedValues()

        self.response_run = True
        self.handle_response_thread = None
        self.start_response()

        setattr(Register, 'write', lambda node, value, _self=self: _self.write_reg(node, value))
        setattr(Register, 'read', lambda node, _self=self: _self.read_reg(node))
        setattr(Register, 'monitor', lambda node, priority=1, task='default', duration=None, samples=None, block=False, _self=self:
                _self.monitor_reg(node, priority=priority, task=task, duration=duration, samples=samples, block=block))
        setattr(Register, 'stop_monitor', lambda node, task=None, _self=self: _self.regmon.remove_listener(node, name=task))


    def start_response(self):
        if self.handle_response_thread is not None and self.handle_response_thread.is_alive():
            return
        self.response_run = True
        self.handle_response_thread = threading.Thread(target=self.handle_response, daemon=True)
        self.handle_response_thread.start()

    def stop_response(self):
        if self.handle_response_thread is None:
            return
        self.response_run = False
        self.resp_queue.put(None)  # wake the blocking get
        self.handle_response_thread.join()
        self.handle_response_thread = None

    def monitor_reg(self, node: Register, priority=1, task="default", duration=None, samples=None, block=False, timeout=1):
        if block and duration is None and samples is None:
            Log.fatal(f"Blocking monitor of {node.get_name()} needs a duration or a sample count")
        start = node.sample_count()
        listener = self.regmon.add_listener(node=node, prio=priority, name=task, duration=duration, samples=samples)
        if not block or listener is None:
            return
        listener.done.wait()

        # the listener ends once the last request is sent, its responses are still on the way
        deadline = time.monotonic() + timeout
        while node.sample_count() - start < listener.issued and time.monotonic() < deadline:
            time.sleep(0.001)
        if node.sample_count() - start < listener.issued:
            Log.warn(f"Only got {node.sample_count() - start} of {listener.issued} samples of {node.get_name()}")

    def read_reg(self, node: Register):
        Log.info(f"Read Register: {node.get_name()}")
        self.req_queue.put(self.ReadRequest(node.get_offset(-1)))
        # self.requests.add(req.addr, node)
        if not node.updated.wait(timeout=1):
            Log.fatal(f"Timeout waiting for register read: {node.get_name()}")
        Log.debug(f"got value: {node.value}")
        return node.value

    def write_reg(self, node: Register, value):
        Log.info(f"Write 0x{value:08X} to Register: {node.get_name()}")
        self.req_queue.put(self.WriteRequest(node.get_offset(-1), value))

    def handle_response(self):
        Log.debug("Handling response")

        while self.response_run:
            resp = self.resp_queue.get(block=True)
            if resp is None:
                break

            debug = Log.debug_enabled()
            if debug:
                Log.debug(f"RX: {resp}")

            try:
                reg = self.project.get_register_by_address(resp.addr)
            except KeyError:
                Log.warn(f"Received response for unknown address 0x{resp.addr:04X}: {resp}")
                continue
            reg.value = resp.value

            if debug:
                Log.debug(f"Name: {reg.get_name()}")
            self.requests.received_response(resp)

            if not self.resp_has_time or resp.time == 0xF or self.prev_sampletime == None:
                sampletime = time.time_ns()
            else:
                sampletime = self.prev_sampletime + (resp.time*(1e9/25000))
            self.prev_sampletime = sampletime
            self.updates.add_update(reg, resp.value, sampletime)
            reg.add_sample(resp.value, sampletime)

        Log.debug("Ending response handler thread")    

