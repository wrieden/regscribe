import time
import threading
from queue import SimpleQueue
from collections import deque
import serial
import serial.tools.list_ports
import termios
import fcntl
import os
import selectors


from regscribe.converter import Log, Project, Register
from regscribe.comm.comm import RegisterMonitor
from regscribe.comm.comm import RequestedValue, ValueUpdates, RequestedValues, ReadRequest, ReadResponse, WriteRequest

from line_profiler import profile


class comm_uart:
    def __init__(self, project: Project, baudrate=115200):
        self.ser = None
        # self.last_handle_time = time.perf_counter()
        self.updates = ValueUpdates()
        self.regmon = RegisterMonitor()
        self.prev_sampletime = None
        # self.read_queue = bytes()
        self.requests = RequestedValues()


        self.baudrate = baudrate
        self.rx_run = True
        self.tx_run = True
        self.handle_tx_thread = None
        self.handle_rx_thread = None

        self.tx_queue : SimpleQueue[ReadRequest | WriteRequest] = SimpleQueue()
        self.project = project

        self.recv_pkgs = 0


        setattr(Register, 'write', lambda node, value, _self=self: _self.write_reg(node, value))
        setattr(Register, 'read', lambda node, _self=self: _self.read_reg(node))
        setattr(Register, 'monitor', lambda node, priority=1, task='default', duration=None, samples=None, _self=self:
                _self.regmon.add_listener(node=node, prio=priority, name=task, duration=duration, samples=samples))
        setattr(Register, 'stop_monitor', lambda node, task=None, _self=self: _self.regmon.remove_listener(node, name=task))



    def connect(self, block):
        for port in serial.tools.list_ports.comports():
            Log.info(f"{port.device} {port.description}")

    
        if self.ser is not None:
            self.ser.close()

        while True:
            # ports = serial.tools.list_ports.grep(r"(com|USB2\.0-Serial)")
            # ports = serial.tools.list_ports.grep(r"(com|USB2\.0-Serial|STLINK-V3 - ST-Link VCP Ctrl)")
            ports = serial.tools.list_ports.grep(r"(USB2\.0-Serial|STLINK-V3 - ST-Link VCP Ctrl|USB Serial|^JTAG Debugger$)")
            # ports = serial.tools.list_ports.grep(r"(ACM1)")
            port = next(ports, None)
            if (port is not None) or (not block):
                break

        # time.sleep(1)

        self.ser = serial.Serial(port=port.device, baudrate=self.baudrate, write_timeout=1, timeout=0, stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS, exclusive=True)
        self.ser.set_low_latency_mode(True)
        fcntl.ioctl(self.ser.fileno(), termios.TIOCEXCL)
        fcntl.flock(self.ser.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.requests.clear()
        Log.info(f"Connected to {self.ser.port}")

        Log.info("Starting UART handler threads")
        self.rx_run = True
        self.tx_run = True
        self.handle_rx_thread = threading.Thread(target=self.handle_rx, daemon=True)
        self.handle_tx_thread = threading.Thread(target=self.handle_tx, daemon=True)
        self.handle_rx_thread.start()
        self.handle_tx_thread.start()

        max_data_size = max(len(ReadRequest(0).__bytes__()), len(WriteRequest(0,0).__bytes__()), len(ReadResponse(bytes(6)).__bytes__()))

        self.pkgs_per_sec = self.ser.baudrate / (max_data_size * (1 + self.ser.bytesize + self.ser.stopbits))
        Log.info(f"Estimated max pkgs/sec: {self.pkgs_per_sec:.2f}, baudrate: {self.ser.baudrate}, max_data_size: {max_data_size}, stopbits: {self.ser.stopbits}")



    def read_reg(self, node: Register):
        Log.info(f"Read Register: {node.get_name()}")
        self.tx_queue.put(ReadRequest(node.get_offset(-1)))
        # self.requests.add(req.addr, node)
        if not node.updated.wait(timeout=1):
            Log.fatal(f"Timeout waiting for register read: {node.get_name()}")
        Log.debug(f"got value: {node.value}")
        return node.value

    def write_reg(self, node: Register, value):
        Log.info(f"Write 0x{value:08X} to Register: {node.get_name()}")
        self.tx_queue.put(WriteRequest(node.get_offset(-1), value))

    @profile
    def handle_rx(self):
        Log.info("Started UART handler thread")
        # goodcnt = 0
        rx_bytearray = bytearray()
        get_reg = self.project.get_register_by_address
        read_uart = self.ser.read_all
        received_response = self.requests.received_response
        # fd = self.ser.fileno()

        # sel = selectors.DefaultSelector()
        # # Register the file descriptor for "Read" events
        # sel.register(fd, selectors.EVENT_READ)

        while self.rx_run:
            # time.sleep(0.01)
            rx_time = time.perf_counter() 
            rx_cpu_time = time.process_time() 
            recv_pkgs = 0
            # self.requests.remove_old_requests(time.time_ns()-1e9)            

            rx_bytearray.extend(read_uart())
            # rx_bytearray.extend(os.read(fd, 4096))
            # if sel.select(timeout=0.1):
            #     rx_bytearray.extend(os.read(fd, 4096))
            # else:
            #     continue

            # while self.ser.in_waiting >= 6:
            while len(rx_bytearray) >= 6:
                rx_data = rx_bytearray[:6]

                if (rx_data[0] & 0x03) != 0x01:
                    Log.warn(f'got wrong data {rx_data[0]}')
                    del rx_bytearray[0]
                    # goodcnt=0
                    # self.requests.remove_old_requests(time.time_ns()-1e9)
                else:
                    del rx_bytearray[:6]

                    resp = ReadResponse(rx_data)

                    # Log.debug(f"RX: {resp}")
                    

                    reg = get_reg(resp.addr)
                    reg.value = resp.value

                    # Log.debug(f"Name: {reg.get_name()}")
                    received_response(resp)
                    
                    if resp.time==0xF or self.prev_sampletime == None:
                        sampletime = time.time_ns()
                    else:
                        sampletime = self.prev_sampletime + (resp.time*(1e9/25000))
                    self.prev_sampletime = sampletime
                    self.updates.add_update(reg, resp.value, sampletime)
    
                    
                    
                    recv_pkgs+=1

            self.recv_pkgs = (self.recv_pkgs + recv_pkgs) & 0xFFFFFFFF

            rx_time = time.perf_counter() - rx_time
            if rx_time > 0.02:
                Log.info(f"RX loop delay too high: {rx_time*1000:.3f} ms ({(time.process_time() - rx_cpu_time)*1000:3f} ms, pkgs recv: {recv_pkgs})")
        Log.info("Ending uart rx handler thread")
    
    @profile
    def handle_tx(self):
        Log.info("Started uart tx handler thread")
        # print(f"runnin {self.ser}")
        # goodcnt = 0
        recv_pkgs_old = 0

        while self.tx_run:
            # time.sleep(0.01)
            tx_time = time.perf_counter()
            tx_cpu_time = time.process_time()

            recv_pkgs_tmp = self.recv_pkgs
            recv_pkgs = (recv_pkgs_tmp - recv_pkgs_old) & 0xFFFFFFFF
            recv_pkgs_old = recv_pkgs_tmp

            tx_bytes = bytes()
            send_pkgs = 0
            # for i in range(min(1000 if self.requests.open_requests()<100 else 0, recv_pkgs * 2 + 10)):
            for send_pkgs in range(100 if self.requests.open_requests()<1000 else 0):
                if not self.tx_queue.empty():
                    req = self.tx_queue.get()
                    self.requests.add_request(req)
                    tx_bytes += bytes(req)
                else:
                    node = self.regmon.get_next()
                    if node is not None:
                        # Log.debug(f'Read Register: {node.get_name()}')
                        req = ReadRequest(node.address)
                        tx_bytes += bytes(req)
                        self.requests.add_request(req)
                    else:
                        break


            if len(tx_bytes) > 0:
                Log.debug(f"Sending bytes: {tx_bytes}")
                self.ser.write(tx_bytes)

            tx_time = time.perf_counter() - tx_time
            if tx_time > 0.02:
                Log.info(f"TX loop delay too high: {tx_time*1000:.3f} ms ({(time.process_time() - tx_cpu_time)*1000:.3f} ms, pkgs sent: {send_pkgs})")
        Log.info("Ending uart tx handler thread")

    def disconnect(self):
        Log.info("Stopping UART handler threads")
        self.rx_run = False
        self.tx_run = False
        self.handle_rx_thread.join()
        self.handle_tx_thread.join()
        if self.ser is not None:
            Log.info("Closing serial port")
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self.ser.close()
