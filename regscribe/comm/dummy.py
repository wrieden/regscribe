import math
import threading
import time

from regscribe.converter import Log, Project, Register, Field
from regscribe.comm.comm import comm, ReadRequestBase, WriteRequestBase, ReadResponseBase


class comm_dummy(comm):
    """Simulated device: answers reads with synthetic waveforms and remembers writes."""

    def __init__(self, project: Project, rate=1000, ReadReq: type | str = ReadRequestBase, WriteReq: type | str = WriteRequestBase, ReadResp: type | str = ReadResponseBase):
        super().__init__(project, ReadReq=ReadReq, WriteReq=WriteReq, ReadResp=ReadResp)

        self.rate = rate
        self.pkgs_per_sec = rate
        self.written: dict[int, int] = dict()
        self.start_time = time.perf_counter()
        self.prev_resp_time = None

        self.run = False
        self.handle_dev_thread = None

    def connect(self, block):
        Log.info(f"Connected to dummy device ({self.rate} pkgs/sec)")
        self.requests.clear()
        self.start_time = time.perf_counter()
        self.prev_resp_time = None
        self.start_response()
        self.run = True
        self.handle_dev_thread = threading.Thread(target=self.handle_dev, daemon=True)
        self.handle_dev_thread.start()

    def disconnect(self):
        Log.info("Stopping dummy device thread")
        self.run = False
        if self.handle_dev_thread is not None:
            self.handle_dev_thread.join()
            self.handle_dev_thread = None
        self.stop_response()

    def handle_dev(self):
        Log.info("Started dummy device thread")
        interval = 1.0 / self.rate if self.rate > 0 else 0.0
        next_pkg = time.perf_counter()

        while self.run:
            now = time.perf_counter()
            if now < next_pkg:
                time.sleep(min(next_pkg - now, 0.005))
                continue
            next_pkg = max(next_pkg + interval, now - 0.1)

            node = None
            if not self.req_queue.empty():
                req = self.req_queue.get()
                if isinstance(req, self.WriteRequest):
                    self.written[req.addr] = req.value
                    continue
                self.requests.add_request(req)
                try:
                    node = self.project.get_register_by_address(req.addr)
                except KeyError:
                    Log.warn(f"Dummy read request for unknown address 0x{req.addr:04X}")
                    continue
            else:
                node = self.regmon.get_next()
                if node is None:
                    time.sleep(0.005)
                    continue
                self.requests.add_request(self.ReadRequest(node.address))

            self.resp_queue.put(self.make_response(node))

        Log.info("Ending dummy device thread")

    def make_response(self, node: Register):
        resp = self.ReadResponse(bytes(64))
        resp.addr = node.address
        resp.value = self.written.get(node.address, self.simulate(node))
        resp.time = self.tick()
        return resp

    def tick(self):
        """4 bit sample distance in 25 kHz ticks, 0xF asks the handler to use the host clock."""
        now = time.perf_counter()
        prev, self.prev_resp_time = self.prev_resp_time, now
        if prev is None:
            return 0xF
        ticks = round((now - prev) * 25000)
        return ticks if 1 <= ticks <= 14 else 0xF

    def simulate(self, node: Register):
        t = time.perf_counter() - self.start_time
        value = 0
        for index, field in enumerate(node.get_children(child_type=Field)):
            lo, hi = field.min, field.max
            if lo is None or hi is None or hi <= lo:
                lo, hi = 0, (1 << field.width) - 1
            shape = (node.address + index) % 4
            freq = 0.25 * (1 + ((node.address + index) % 5))
            phase = (t * freq) % 1.0

            if shape == 0:
                level = 0.5 + 0.5 * math.sin(2 * math.pi * phase)
            elif shape == 1:
                level = phase
            elif shape == 2:
                level = 1.0 - abs(2.0 * phase - 1.0)
            else:
                level = float(phase < 0.5)

            raw = int(round(lo + (hi - lo) * level))
            value |= (raw << field.offset) & field.mask

        return value & ((1 << (node.width or 32)) - 1)
