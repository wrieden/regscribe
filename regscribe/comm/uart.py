import time
import threading
import serial
import serial.tools.list_ports
import termios
import fcntl


from regscribe.converter import Log, Project
from regscribe.comm.comm import comm, ReadRequestBase, WriteRequestBase, ReadResponseBase

from line_profiler import profile


def median(values):
    return sorted(values)[len(values) // 2]


class comm_uart(comm):
    def __init__(self, project: Project, baudrate=115200, ReadReq:type | str = ReadRequestBase, WriteReq:type | str = WriteRequestBase, ReadResp:type | str = ReadResponseBase):
        super().__init__(project, ReadReq=ReadReq, WriteReq=WriteReq, ReadResp=ReadResp)

        self.ser = None
        self.baudrate = baudrate

        self.rx_run = True
        self.tx_run = True
        self.handle_tx_thread = None
        self.handle_rx_thread = None

        # how many read requests may be in flight before responses start getting dropped
        self.max_inflight = 40
        # how many requests are packed into a single write, above this a write stops paying off
        self.max_tx_batch = 40

        self.recv_pkgs = 0



    def connect(self, block, max_inflight=None, max_tx_batch=None):
        for port in serial.tools.list_ports.comports():
            Log.info(f"{port.device} {port.description}")

        if self.ser is not None:
            self.ser.close()

        while True:
            # ports = serial.tools.list_ports.grep(r"(com|USB2\.0-Serial)")
            # ports = serial.tools.list_ports.grep(r"(com|USB2\.0-Serial|STLINK-V3 - ST-Link VCP Ctrl)")
            ports = serial.tools.list_ports.grep(r"(USB2\.0-Serial|STLINK-V3 - ST-Link VCP Ctrl|USB Serial|^JTAG Debugger$|CP2102|FT232H)")
            # ports = serial.tools.list_ports.grep(r"(ACM1)")
            port = next(ports, None)
            if (port is not None) or (not block):
                break
            time.sleep(0.5)

        if port is None:
            Log.error("No matching serial port found")
            return

        self.ser = serial.Serial(port=port.device, baudrate=self.baudrate, write_timeout=1, timeout=0, stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS, exclusive=True)
        self.ser.set_low_latency_mode(True)
        fcntl.ioctl(self.ser.fileno(), termios.TIOCEXCL)
        fcntl.flock(self.ser.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.requests.clear()
        Log.info(f"Connected to {self.ser.port}")

        max_data_size = max(len(self.ReadRequest(0).__bytes__()), len(self.WriteRequest(0,0).__bytes__()), len(bytes(self.ReadResponse(bytes(64)))))

        self.pkgs_per_sec = self.ser.baudrate / (max_data_size * (1 + self.ser.bytesize + self.ser.stopbits))
        Log.info(f"Estimated max pkgs/sec: {self.pkgs_per_sec:.2f}, baudrate: {self.ser.baudrate}, max_data_size: {max_data_size}, stopbits: {self.ser.stopbits}")

        if max_inflight is None:
            self.max_inflight = self.probe_line_capacity()
        else:
            self.max_inflight = max_inflight
            Log.info(f"Line capacity probe skipped, using {self.max_inflight} requests in flight")

        if max_tx_batch is None:
            self.max_tx_batch = self.probe_write_batch(self.max_inflight)
        else:
            self.max_tx_batch = max_tx_batch
            Log.info(f"Write batch probe skipped, using {self.max_tx_batch} requests per write")

        Log.info("Starting UART handler threads")
        self.rx_run = True
        self.tx_run = True
        self.start_response()
        self.handle_rx_thread = threading.Thread(target=self.handle_rx, daemon=True)
        self.handle_tx_thread = threading.Thread(target=self.handle_tx, daemon=True)
        self.handle_rx_thread.start()
        self.handle_tx_thread.start()

    def _drain(self):
        # let stragglers of an incomplete burst arrive, flushing mid-datagram would fake a loss
        time.sleep(0.05)
        self.ser.reset_input_buffer()

    def _count_responses(self, data: bytearray, addr: int, resp_bytes: int):
        """Count intact responses for `addr`, resyncing byte-wise like the rx loop does."""
        count = 0
        pos = 0
        while pos + resp_bytes <= len(data):
            resp = self.ReadResponse(data[pos:pos + resp_bytes])
            if resp.valid() and resp.addr == addr:
                count += 1
                pos += resp_bytes
            else:
                pos += 1
        return count

    def probe_line_capacity(self, addr=0, max_burst=4096, duration=1.0, trial_duration=0.3, repeats=3, step=2, min_gain=0.02, plateau_tolerance=0.05, safety_margin=0.25):
        """Find how many read requests may be in flight before the link starts dropping responses.

        Runs before the handler threads start: bursts of read requests to `addr` are written in
        one go and the answers are counted. Burst sizes are ramped by `step` until the gain of a
        step falls below `min_gain`, then the smallest size that is within `plateau_tolerance` of
        the fastest one wins and is confirmed for `duration`. Picking against the plateau instead
        of against the previous step is what makes the result reproducible: near saturation the
        steps are only a few percent apart, so a step-wise threshold lands one size off whenever
        the measurement drifts, while the plateau moves by well under a percent. How many of those
        requests are worth packing into a single write is a separate question, see
        `probe_write_batch`.

        Only loss free sizes are accepted: the first lost response ends the ramp, and a loss during
        the confirmation steps the size back down.

        The rate is the median of the per burst round times, measured around nothing but the write
        and the read - the responses are decoded after the measurement - so neither the decoding
        cost nor a scheduler hiccup enters the result. Every size is measured `repeats` times and
        the median of those passes decides.
        """
        req = bytes(self.ReadRequest(addr))
        resp_bytes = len(bytes(self.ReadResponse(bytes(64))))
        byte_time = (1 + self.ser.bytesize + int(self.ser.stopbits)) / self.ser.baudrate

        def single_pass(burst, seconds):
            buf = req * burst
            expected = burst * resp_bytes
            # blocking read returns as soon as the burst is complete, only a loss costs the timeout
            self.ser.timeout = burst * (len(req) + resp_bytes) * byte_time + 0.05

            sent = 0
            round_times = []
            chunks = []
            self._drain()
            deadline = time.perf_counter() + seconds
            while time.perf_counter() < deadline:
                start = time.perf_counter()
                self.ser.write(buf)
                data = self.ser.read(expected)
                round_time = time.perf_counter() - start
                sent += burst
                chunks.append(data)
                if len(data) == expected:
                    round_times.append(round_time)
                else:
                    # an incomplete burst ran into the read timeout, that is not a rate
                    self._drain()

            recv = sum(self._count_responses(bytearray(c), addr, resp_bytes) for c in chunks)
            pkgs_per_sec = burst / median(round_times) if round_times else 0.0
            Log.debug(f"Line capacity probe: burst {burst}: {recv}/{sent} responses in {len(chunks)} bursts, "
                      f"{sent-recv} lost, {pkgs_per_sec:.0f} pkgs/s")
            return sent, sent - recv, pkgs_per_sec

        def sustained(burst, seconds, passes):
            sent = lost = 0
            rates = []
            for _ in range(passes):
                s, l, rate = single_pass(burst, seconds)
                sent += s
                lost += l
                rates.append(rate)
            pkgs_per_sec = median(rates)
            Log.info(f"Line capacity probe: burst {burst}: {sent-lost}/{sent} responses in {passes} passes, "
                     f"{lost} lost, {pkgs_per_sec:.0f} pkgs/s")
            return sent, lost, pkgs_per_sec

        # settle the link: the first requests after opening the port can be swallowed
        self.ser.timeout = 0.1
        self.ser.write(req)
        self.ser.read(resp_bytes)

        rates = {}
        best_pkgs_per_sec = 0.0
        burst = 1
        while burst <= max_burst:
            sent, lost, pkgs_per_sec = sustained(burst, trial_duration, repeats)
            if lost:
                break
            rates[burst] = pkgs_per_sec
            # more requests in flight only help until the link is saturated
            if pkgs_per_sec < best_pkgs_per_sec * (1 + min_gain):
                break
            best_pkgs_per_sec = pkgs_per_sec
            burst *= step

        good = 1
        if rates:
            plateau = max(rates.values()) * (1 - plateau_tolerance)
            good = min(b for b, rate in rates.items() if rate >= plateau)

        # confirm over the long haul, so slow drifts and rare losses show up too
        while True:
            sent, lost, pkgs_per_sec = sustained(good, duration, 1)
            if not lost or good == 1:
                break
            Log.warn(f"Line capacity probe: burst {good} lost {lost} of {sent} responses")
            good //= step

        limit = max(1, int(good * (1 - safety_margin)))

        self.ser.timeout = 0
        self.ser.reset_input_buffer()
        self.requests.clear()
        Log.info(f"Line capacity probe: burst {good} confirmed at {pkgs_per_sec:.0f} pkgs/s, "
                 f"using {limit} requests in flight")
        return limit

    def probe_write_batch(self, inflight, addr=0, duration=0.2, repeats=3, step=2, plateau_tolerance=0.05):
        """Find how many requests are worth packing into one write at the given in-flight window.

        The capacity probe cannot answer this: it stops and waits for every burst, so there the
        write size and the window are the same number. Here the window is held at `inflight` - the
        pipeline is primed and every batch of responses is answered by a batch of new requests -
        and only the write size is swept, which is what the tx loop actually varies.

        A write costs a syscall and, on a USB bridge, a frame, so small batches pay that per
        request while large ones amortise it. The smallest size within `plateau_tolerance` of the
        fastest wins, since a bigger write past the plateau only adds latency. Sizes that lose a
        response are rejected outright.
        """
        req = bytes(self.ReadRequest(addr))
        resp_bytes = len(bytes(self.ReadResponse(bytes(64))))
        byte_time = (1 + self.ser.bytesize + int(self.ser.stopbits)) / self.ser.baudrate

        def single_pass(batch, seconds):
            buf = req * batch
            expected = batch * resp_bytes
            # the whole window may be in the link before the requested batch shows up
            self.ser.timeout = (inflight + batch) * (len(req) + resp_bytes) * byte_time + 0.05

            self._drain()
            self.ser.write(req * inflight)
            sent = inflight
            round_times = []
            data = bytearray()
            complete = True
            deadline = time.perf_counter() + seconds
            while complete and time.perf_counter() < deadline:
                start = time.perf_counter()
                chunk = self.ser.read(expected)
                # topping up what was just consumed is what keeps the window at `inflight`
                self.ser.write(buf)
                round_times.append(time.perf_counter() - start)
                sent += batch
                data.extend(chunk)
                complete = len(chunk) == expected

            data.extend(self.ser.read(inflight * resp_bytes))
            recv = self._count_responses(data, addr, resp_bytes)
            if not complete:
                self._drain()

            pkgs_per_sec = batch / median(round_times) if complete and round_times else 0.0
            Log.debug(f"Write batch probe: batch {batch}: {recv}/{sent} responses, "
                      f"{sent-recv} lost, {pkgs_per_sec:.0f} pkgs/s")
            return sent, sent - recv, pkgs_per_sec

        def sustained(batch, seconds, passes):
            sent = lost = 0
            rates = []
            for _ in range(passes):
                s, l, rate = single_pass(batch, seconds)
                sent += s
                lost += l
                rates.append(rate)
            pkgs_per_sec = median(rates)
            Log.info(f"Write batch probe: batch {batch}: {sent-lost}/{sent} responses in {passes} passes, "
                     f"{lost} lost, {pkgs_per_sec:.0f} pkgs/s")
            return sent, lost, pkgs_per_sec

        batches = []
        batch = 1
        while batch < inflight:
            batches.append(batch)
            batch *= step
        batches.append(inflight)

        rates = {}
        for batch in batches:
            sent, lost, pkgs_per_sec = sustained(batch, duration, repeats)
            if not lost:
                rates[batch] = pkgs_per_sec

        good = 1
        if rates:
            plateau = max(rates.values()) * (1 - plateau_tolerance)
            good = min(b for b, rate in rates.items() if rate >= plateau)

        self.ser.timeout = 0
        self.ser.reset_input_buffer()
        self.requests.clear()
        Log.info(f"Write batch probe: {good} requests per write at {rates.get(good, 0):.0f} pkgs/s, "
                 f"{max(rates.values(), default=0):.0f} pkgs/s at best")
        return good

    def disconnect(self):
        Log.info("Stopping UART handler threads")
        self.rx_run = False
        self.tx_run = False
        if self.handle_rx_thread is not None:
            self.handle_rx_thread.join()
        if self.handle_tx_thread is not None:
            self.handle_tx_thread.join()
        self.stop_response()
        if self.ser is not None:
            Log.info("Closing serial port")
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self.ser.close()

    @profile
    def handle_rx(self):
        Log.info("Started handle_rx thread")
        # goodcnt = 0
        rx_bytearray = bytearray()
        # get_reg = self.project.get_register_by_address
        # __bytes__ of a dummy response reports the real datagram size; the oversized input is just padding
        resp_bytes = len(bytes(self.ReadResponse(bytes(64))))
        Log.info(f"RX response size: {resp_bytes} bytes")

        ReadResponse = self.ReadResponse
        put_response = self.resp_queue.put
        read_all = self.ser.read_all

        while self.rx_run:
            # time.sleep(0.01)
            rx_time = time.perf_counter() 
            rx_cpu_time = time.process_time() 
            recv_pkgs = 0
            debug = Log.debug_enabled()
            # self.requests.remove_old_requests(time.time_ns()-1e9)            

            rx_bytearray.extend(read_all())
            while len(rx_bytearray) >= resp_bytes:
                resp = ReadResponse(rx_bytearray[:resp_bytes])

                if resp.valid():
                    del rx_bytearray[:resp_bytes]
                    put_response(resp)
                    if debug:
                        Log.debug(f"RX: {resp}")
                    recv_pkgs+=1
                else:           
                    Log.warn(f'got wrong data {resp}')
                    del rx_bytearray[0]

            self.recv_pkgs = (self.recv_pkgs + recv_pkgs) & 0xFFFFFFFF

            rx_time = time.perf_counter() - rx_time
            if rx_time > 0.02:
                Log.info(f"RX loop delay too high: {rx_time*1000:.3f} ms ({(time.process_time() - rx_cpu_time)*1000:3f} ms, pkgs recv: {recv_pkgs})")
        Log.info("Ending uart rx handler thread")
    
    @profile
    def handle_tx(self):
        Log.info("Started handle_tx thread")
        # print(f"runnin {self.ser}")
        # goodcnt = 0
        recv_pkgs_old = 0

        while self.tx_run:
            # time.sleep(0.01)
            tx_time = time.perf_counter()
            tx_cpu_time = time.process_time()
            debug = Log.debug_enabled()

            recv_pkgs_tmp = self.recv_pkgs
            recv_pkgs = (recv_pkgs_tmp - recv_pkgs_old) & 0xFFFFFFFF
            recv_pkgs_old = recv_pkgs_tmp

            tx_bytes = []
            send_pkgs = 0
            # for i in range(min(1000 if self.requests.open_requests()<100 else 0, recv_pkgs * 2 + 10)):
            for send_pkgs in range(min(self.max_tx_batch, self.max_inflight - self.requests.open_requests())):
                if not self.req_queue.empty():
                    req = self.req_queue.get()
                    self.requests.add_request(req)
                    if debug:
                        Log.debug(f"TX: {req}")
                    tx_bytes.append(bytes(req))
                else:
                    node = self.regmon.get_next()
                    if node is not None:
                        # Log.debug(f'Read Register: {node.get_name()}')
                        req = self.ReadRequest(node.address)
                        tx_bytes.append(bytes(req))
                        if debug:
                            Log.debug(f"TX: {req}")
                        self.requests.add_request(req)
                    else:
                        break


            if tx_bytes:
                # Log.debug(f"Sending bytes: {tx_bytes.hex()}")
                self.ser.write(b"".join(tx_bytes))

            tx_time = time.perf_counter() - tx_time
            if tx_time > 0.02:
                Log.info(f"TX loop delay too high: {tx_time*1000:.3f} ms ({(time.process_time() - tx_cpu_time)*1000:.3f} ms, pkgs sent: {send_pkgs})")
        Log.info("Ending uart tx handler thread")


