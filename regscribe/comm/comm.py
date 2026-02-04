from regscribe.converter import Log, Register
import time
from queue import SimpleQueue
from threading import Lock


class RegisterMonitor:
    def __init__(self):
        self.monitored = dict()
        self.it = iter(self.monitored.values())
        self.lock = Lock()

    def add_listener(self, node, name, prio, samples=None, duration=None):
        if prio == 0:
            self.remove_listener(node, name)
        else:
            self.lock.acquire()
            mon = self.monitored.pop(node, Monitored(node))
            mon.add_listener(name, prio)
            self.monitored[node] = mon

            self.it = iter(self.monitored.values())
            self.lock.release()


    def remove_listener(self, node, name):
        self.lock.acquire()
        mon = self.monitored.pop(node, Monitored(node))
        mon.remove_listener(name)
        if mon.has_listener():
            self.monitored[node] = mon

        self.it = iter(self.monitored.values())
        self.lock.release()


    def get_next(self) -> Register | None: # may be called in a thread...
        self.lock.acquire()
        while True:
            mon = next(self.it, None)
            if mon is None:
                self.it = iter(self.monitored.values())
                mon = next(self.it, None)
            if mon is None:
                self.lock.release()
                return None

            if mon.counter <= 1:
                mon.reset_counter()
                self.lock.release()
                return mon.node
            else:
                mon.counter -= 1

class Monitored:
    def __init__(self, node):
        self.node = node
        self.priority = dict()
        self.counter = 1

    def add_listener(self, name, priority):
        self.priority[name] = priority
        self.counter = min([self.counter, self.lowest_priority()])

    def reset_counter(self):
        self.counter = self.lowest_priority()

    def remove_listener(self, name):
        if name is None:
            self.priority.clear()
        else:
            self.priority.pop(name, None)

    def lowest_priority(self):
        return min(self.priority.values())

    def has_listener(self):
        return bool(self.priority)



class ValueUpdate:
    def __init__(self, value, time):
        self.value = value
        self.time = time


class ValueUpdates:
    def __init__(self):
        self.updates = dict()

    def add_update(self, node, value, time):
        if node is not None:
            update = ValueUpdate(value, time)
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
            d[node.get_id()] = list()
            for update in updates:
                d[node.get_id()].append({"value": update.value, "time": update.time})
        return d


class WriteRequest:
    def __init__(self, addr, value):
        self.addr = addr
        self.value = value

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

    def __str__(self):
        return f"0x{self.addr:04X} -> 0x{self.value:08X}"


class ReadRequest:
    def __init__(self, addr):
        self.addr = addr

    def __bytes__(self):
        return bytes(bytearray([0x01 | ((self.addr & 0x03) << 6), (self.addr >> 2) & 0xFF]))


class ReadResponse:
    def __init__(self, bytes: bytes | bytearray):
        resp = bytearray(bytes)
        self.sync = resp[0] & 0x03
        self.time = (resp[0] >> 2) & 0x0F
        self.addr = (resp[1] << 2) | ((resp[0] & 0xC0) >> 6)
        self.value = resp[5] << 24 | resp[4] << 16 | resp[3] << 8 | resp[2]

    def __str__(self):
        return f"0x{self.addr:04X} <- 0x{self.value:08X}, {self.time}t, {self.sync}s"

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


class RequestedValue:
    def __init__(self, request, time):
        self.addr = request.addr
        # self.node = node
        self.time = time


class RequestedValues:

    def __init__(self):
        self.requests : SimpleQueue[RequestedValue] = SimpleQueue()

    def add_request(self, request: WriteRequest | ReadRequest, t=None):
        if isinstance(request, ReadRequest):
            if t is None:
                t = time.time_ns()
            self.requests.put(RequestedValue(request, t))

    def received_response(self, response: ReadResponse):
        req = self.requests.get()
        if req.addr != response.addr:
            Log.warn(f"Received response for address 0x{response.addr:04X} but expected 0x{req.addr:04X}")
        else:
            Log.debug(f"Matched response for address 0x{response.addr:04X}")

    def open_requests(self):
        return self.requests.qsize()

    def clear(self):
        try:
            while True:
                self.requests.get(block=False)
        except:
            pass