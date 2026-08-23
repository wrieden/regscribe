import time
import threading
import serial
import serial.tools.list_ports
import termios
import fcntl


from regscribe.converter import Log, Project
from regscribe.comm.comm import comm, ReadRequestBase, WriteRequestBase, ReadResponseBase

from line_profiler import profile


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

        self.recv_pkgs = 0



    def connect(self, block, max_inflight=None):
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

        Log.info("Starting UART handler threads")
        self.rx_run = True
        self.tx_run = True
        self.start_response()
        self.handle_rx_thread = threading.Thread(target=self.handle_rx, daemon=True)
        self.handle_tx_thread = threading.Thread(target=self.handle_tx, daemon=True)
        self.handle_rx_thread.start()
        self.handle_tx_thread.start()

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

    def probe_line_capacity(self, addr=0, max_burst=4096, duration=1.0, trial_duration=0.15, step=2, min_gain=0.5, max_loss_rate=1e-3, safety_margin=0.25):
        """Find how many read requests may be in flight before the link starts dropping responses.

        Runs before the handler threads start: bursts of read requests to `addr` are written in
        one go and the answers are counted. Burst sizes are ramped by `step` and the smallest one
        that reaches the throughput plateau wins, which is then confirmed for `duration`.

        Selecting on throughput instead of on the first lost packet is what makes the result
        reproducible: the link loses a packet every few thousand transfers at any burst size, so
        "largest loss free burst" is a coin flip, while throughput over the complete bursts is
        repeatable within a few percent. The coarse ladder and `min_gain` keep the comparison well
        outside that noise. Losses still veto a size through `max_loss_rate`.
        """
        req = bytes(self.ReadRequest(addr))
        resp_bytes = len(bytes(self.ReadResponse(bytes(64))))
        byte_time = (1 + self.ser.bytesize + int(self.ser.stopbits)) / self.ser.baudrate

        def run_burst(burst):
            self.ser.write(req * burst)

            expected = burst * resp_bytes
            # blocking read returns as soon as the burst is complete, only a loss costs the timeout
            self.ser.timeout = burst * (len(req) + resp_bytes) * byte_time + 0.05
            data = self.ser.read(expected)

            return self._count_responses(bytearray(data), addr, resp_bytes), len(data) == expected

        def drain():
            # let stragglers of an incomplete burst arrive, flushing mid-datagram would fake a loss
            time.sleep(0.05)
            self.ser.reset_input_buffer()

        def sustained(burst, seconds):
            sent = recv = rounds = clean_pkgs = 0
            clean_time = 0.0
            drain()
            start = time.perf_counter()
            while time.perf_counter() - start < seconds:
                round_start = time.perf_counter()
                got, complete = run_burst(burst)
                sent += burst
                recv += got
                rounds += 1
                if complete:
                    # timing only the complete bursts keeps the read timeout of a lost packet
                    # out of the throughput, which is what makes this measurement repeatable
                    clean_pkgs += burst
                    clean_time += time.perf_counter() - round_start
                else:
                    drain()

            pkgs_per_sec = clean_pkgs / clean_time if clean_time else 0.0
            Log.info(f"Line capacity probe: burst {burst}: {recv}/{sent} responses in {rounds} bursts, "
                     f"{sent-recv} lost, {pkgs_per_sec:.0f} pkgs/s")
            return sent, recv, pkgs_per_sec

        # settle the link: the first requests after opening the port can be swallowed
        run_burst(1)

        good = 1
        best_pkgs_per_sec = 0.0
        burst = 1
        while burst <= max_burst:
            sent, recv, pkgs_per_sec = sustained(burst, trial_duration)
            # more requests in flight only help until the link is saturated, and past the knee the
            # steps are worth ~10% each - far too close together to tell apart reliably
            if pkgs_per_sec < best_pkgs_per_sec * (1 + min_gain):
                break
            best_pkgs_per_sec = pkgs_per_sec
            good = burst
            burst *= step

        # confirm over the long haul, so slow drifts and rare losses show up too
        while True:
            sent, recv, pkgs_per_sec = sustained(good, duration)
            if sent - recv <= 1 + max_loss_rate * sent or good == 1:
                break
            Log.warn(f"Line capacity probe: burst {good} loses {(sent-recv)/sent:.1e} of the responses")
            good //= step

        limit = max(1, int(good * (1 - safety_margin)))

        self.ser.timeout = 0
        self.ser.reset_input_buffer()
        self.requests.clear()
        Log.info(f"Line capacity probe: burst {good} confirmed at {pkgs_per_sec:.0f} pkgs/s, "
                 f"using {limit} requests in flight")
        return limit

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
            for send_pkgs in range(10 if self.requests.open_requests() < self.max_inflight else 0):
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


