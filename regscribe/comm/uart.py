import time
import threading
from queue import Queue
import serial
import serial.tools.list_ports

from regscribe.converter import Log, Project, Register
from regscribe.comm.comm import RegisterMonitor
from regscribe.comm.comm import RequestedValue, ValueUpdates, RequestedValues, ReadRequest, ReadResponse, WriteRequest

class comm_uart:
    def __init__(self, project: Project):
        self.ser = None
        self.last_handle_time = time.perf_counter()
        self.updates = ValueUpdates()
        self.regmon = RegisterMonitor()
        self.prev_sampletime = None
        self.read_queue = bytes()
        self.requests = RequestedValues()

        self.handle_rx_stop = threading.Event()
        self.handle_tx_stop = threading.Event()
        self.handle_tx_thread = None
        self.handle_rx_thread = None

        self.tx_queue = Queue()
        self.project = project

        self.recv_pkgs = 0


        setattr(Register, 'write', lambda node, value, _self=self: _self.write_reg(node, value))
        setattr(Register, 'read', lambda node, _self=self: _self.read_reg(node))
        setattr(Register, 'monitor', lambda node, _self=self: _self.regmon.add_listener(node, "test", 1))



    def connect(self, block):
        for port in serial.tools.list_ports.comports():
            Log.info(f"{port.device} {port.description}")

        try:
            if self.ser is not None:
                self.ser.close()

            while True:
                # ports = serial.tools.list_ports.grep(r"(com|USB2\.0-Serial)")
                # ports = serial.tools.list_ports.grep(r"(com|USB2\.0-Serial|STLINK-V3 - ST-Link VCP Ctrl)")
                ports = serial.tools.list_ports.grep(r"(USB2\.0-Serial|STLINK-V3 - ST-Link VCP Ctrl)")
                # ports = serial.tools.list_ports.grep(r"(ACM1)")
                port = next(ports, None)
                if (port is not None) or (not block):
                    break

            time.sleep(1)

            self.ser = serial.Serial(port=port.device, baudrate=2000000, write_timeout=1, timeout=0)
            self.requests.clear()
            Log.info(f"Connected to {self.ser.port}")

            Log.info("Starting UART handler threads")
            self.handle_rx_thread = threading.Thread(target=self.handle_rx, daemon=True)
            self.handle_tx_thread = threading.Thread(target=self.handle_tx, daemon=True)
            self.handle_rx_thread.start()
            self.handle_tx_thread.start()

        except Exception:
            print("could not open serial port")

    def read_reg(self, node: Register):
        Log.debug(f"Read Register: {node.get_name()}")
        self.tx_queue.put(ReadRequest(node.get_offset(-1)))
        # self.requests.add(req.addr, node)
        node.updated.wait()
        Log.debug(f"got value: {node.value}")
        return node.value

    def write_reg(self, node, value):
        Log.debug(f"write_reg: {node.get_name()}, 0x{value:08X}")
        self.tx_queue.put(WriteRequest(node.get_offset(-1), value))

    def handle_rx(self):
        Log.info("Started UART handler thread")
        # goodcnt = 0
        while not self.handle_rx_stop.is_set():
            # time.sleep(0.01)
            # self.requests.remove_old_requests(time.time_ns()-1e9)

            # nodes = []
            while self.ser.in_waiting >= 6:
                cmd = self.ser.read(1)
                if (cmd[0] & 0x03) != 0x01:
                    Log.warn(f'got wrong data {cmd[0]}')
                    # goodcnt=0
                    # self.requests.remove_old_requests(time.time_ns()-1e9)
                    continue

                resp = ReadResponse(cmd + self.ser.read(5))
                Log.debug(f"RX: {resp}")
                self.recv_pkgs = (self.recv_pkgs + 1) & 0xFFFFFFFF

                reg = self.project.get_register_by_address(resp.addr)
                reg.value = resp.value

                # Log.debug(f"Name: {reg.get_name()}")

                # node = self.requests.remove(resp.addr)
                # if node is not None and (resp.time!=0xF or node not in nodes):
                #     # print(f'RDe req: {resp.addr} {resp.value} {node}')
                #     if resp.time==0xF or self.prev_sampletime == None:
                #         sampletime = time.time_ns()
                #     else:
                #         sampletime = self.prev_sampletime + (resp.time*(1e9/25000))

                #     # sampletime = time.time_ns()
                #     # Log.info(f'st: {sampletime} dt: {Date.now()}')
                #     self.prev_sampletime = sampletime

                #     self.updates.add_update(node, resp.value, sampletime)
                #     node.set_value(resp.value)
                #     node.updated.set()
                #     node.updated.clear()
                #     #print(f'{self.ser.in_waiting}')
                #     nodes.append(node)
                #     goodcnt+=1
                #     recv_pkgs+=1

    def handle_tx(self):
        Log.info("Started uart tx handler thread")
        # print(f"runnin {self.ser}")
        # goodcnt = 0
        recv_pkgs_old = 0
        
        while not self.handle_tx_stop.is_set():
            # time.sleep(0.01)
            
            recv_pkgs_tmp = self.recv_pkgs
            recv_pkgs = (recv_pkgs_tmp - recv_pkgs_old) & 0xFFFFFFFF
            recv_pkgs_old = recv_pkgs_tmp

            tx_bytes = bytes()
            for i in range(min(1000, recv_pkgs * 2 + 10)):
                if not self.tx_queue.empty():
                    tx_bytes += bytes(self.tx_queue.get())
                    self.tx_queue.task_done()
                # elif not self.tx_queue.empty():
                #     tx_bytes += bytes(self.tx_queue.get())
                #     self.tx_queue.task_done()
                else:
                    node = self.regmon.get_next()
                    if node is not None:
                        # Log.debug(f'Read Register: {node.get_name()}')
                        req = ReadRequest(node.address)
                        tx_bytes += bytes(req)
                        self.requests.add(req.addr, node)
                    else:
                        break


            if len(tx_bytes) > 0:
                Log.debug(f"Sending bytes: {tx_bytes}")
                self.ser.write(tx_bytes)

    def disconnect(self):
        Log.info("Stopping UART handler threads")
        self.handle_rx_stop.set()
        self.handle_tx_stop.set()
        self.handle_rx_thread.join()
        self.handle_tx_thread.join()
        if self.ser is not None:
            Log.info("Closing serial port")
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self.ser.close()
