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


        self.recv_pkgs = 0



    def connect(self, block):
        for port in serial.tools.list_ports.comports():
            Log.info(f"{port.device} {port.description}")

        if self.ser is not None:
            self.ser.close()

        while True:
            # ports = serial.tools.list_ports.grep(r"(com|USB2\.0-Serial)")
            # ports = serial.tools.list_ports.grep(r"(com|USB2\.0-Serial|STLINK-V3 - ST-Link VCP Ctrl)")
            ports = serial.tools.list_ports.grep(r"(USB2\.0-Serial|STLINK-V3 - ST-Link VCP Ctrl|USB Serial|^JTAG Debugger$|CP2102)")
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

        Log.info("Starting UART handler threads")
        self.rx_run = True
        self.tx_run = True
        self.start_response()
        self.handle_rx_thread = threading.Thread(target=self.handle_rx, daemon=True)
        self.handle_tx_thread = threading.Thread(target=self.handle_tx, daemon=True)
        self.handle_rx_thread.start()
        self.handle_tx_thread.start()

        max_data_size = max(len(self.ReadRequest(0).__bytes__()), len(self.WriteRequest(0,0).__bytes__()), len(bytes(self.ReadResponse(bytes(64)))))

        self.pkgs_per_sec = self.ser.baudrate / (max_data_size * (1 + self.ser.bytesize + self.ser.stopbits))
        Log.info(f"Estimated max pkgs/sec: {self.pkgs_per_sec:.2f}, baudrate: {self.ser.baudrate}, max_data_size: {max_data_size}, stopbits: {self.ser.stopbits}")

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
            for send_pkgs in range(100 if self.requests.open_requests()<1000 else 0):
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


