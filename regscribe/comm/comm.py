from regscribe.converter import Log
import time
from collections import deque



class RegisterMonitor:
    def __init__(self):
        self.monitored = dict()
        self.it = iter(self.monitored.values())


    def add_listener(self, node, name, prio):
        if prio == 0:
            self.remove_listener(node, name)
            return

        mon = self.monitored.pop(node, Monitored(node))
        mon.add_listener(name, prio)
        self.monitored[node] = mon

        self.it = iter(self.monitored.values())


    def remove_listener(self, node, name):
        mon = self.monitored.pop(node, Monitored(node))
        mon.remove_listener(name)
        if mon.has_listener():
            self.monitored[node] = mon

        self.it = iter(self.monitored.values())


    def get_next(self):
        while True:
            mon = next(self.it, None)
            if mon is None:
                self.it = iter(self.monitored.values())
                mon = next(self.it, None)
            if mon is None:
                return None

            if mon.counter <= 1:
                mon.reset_counter()
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
    def __init__(self, bytes):
        resp = bytearray(bytes)
        self.sync = resp[0] & 0x03
        self.time = (resp[0] >> 2) & 0x0F
        self.addr = (resp[1] << 2) | ((resp[0] & 0xC0) >> 6)
        self.value = resp[5] << 24 | resp[4] << 16 | resp[3] << 8 | resp[2]

    def __str__(self):
        return f"0x{self.addr:04X} <- 0x{self.value:08X}, {self.time}t, {self.sync}s"


class RequestedValue:
    def __init__(self, addr, node, time):
        self.addr = addr
        self.node = node
        self.time = time


class RequestedValues:

    def __init__(self):
        self.requests = dict()
        self.active_requests = 0
        self.reqorder = deque()

    def add(self, addr, node, t=None):
        if t is None:
            t = time.time_ns()
        req = RequestedValue(addr, node, t)
        self.reqorder.append(req)
        # if addr in self.requests:
        #     self.requests[addr].append(req)
        # else:
        #     self.requests[addr] = [req]
        self.active_requests += 1

    def remove(self, addr):

        try:
            for req in self.reqorder:
                if req.addr == addr:
                    self.reqorder.remove(req)
                    return req.node

        # try:
        #     node = self.requests[addr].pop().node
        #     if (len(self.requests[addr]) == 0):
        #         del self.requests[addr]
        #     self.active_requests-=1
        except Exception as e:
            Log.info(f"Unable to resolve id {addr} to a node!\n {e}")
            return None

        # return node

    def remove_old_requests(self, time):
        for req in self.reqorder:
            if req.time < time:
                Log.info(f"Removed request for {req.addr} from time {req.time} at {time}")
                self.reqorder.remove(req)
                return

        # try:
        #     node = self.requests[addr].pop().node
        #     if (len(self.requests[addr]) == 0):
        #         del self.requests[addr]
        #     self.active_requests-=1

        # except Exception as e:
        #     Log.info(f'Unable to resolve id {addr} to a node!\n {e}')
        #     node = None

        # for key in list(self.requests.keys()):
        #     for req in reversed(self.requests[key]):
        #         if req.time < time:
        #             self.requests[key].remove(req)
        #             self.active_requests-=1
        #             Log.info(f'Reqeust id {key} timed out!')
        #     if(len(self.requests[key])==0):
        #         del self.requests[key]

    def num(self):
        return len(self.reqorder)

        return self.active_requests
        num = 0
        for reqs in self.requests.values():
            num += len(reqs)
        return num

    def clear(self):
        self.reqorder.clear()
        return

        self.requests = dict()
        self.active_requests = 0