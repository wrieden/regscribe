import serial
import serial.tools.list_ports
import time
import asyncio

from regscribe.converter import Log, Project

from regscribe.comm import RegisterMonitor
from collections import deque
import asyncio

import threading

from queue import Queue


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


class comm_uart:
    def __init__(self, project: Project):
        self.ser = None
        self.last_handle_time = time.perf_counter()
        self.updates = ValueUpdates()
        self.regmon = RegisterMonitor()
        self.prev_sampletime = None
        self.read_queue = bytes()
        self.requests = RequestedValues()
        self.async_handle = None

        self.handle_stop = threading.Event()
        self.handle_thread = None
        self.rx_queue = Queue()
        self.tx_queue = Queue()
        self.tx_prio_queue = Queue()
        self.project = project

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

            self.ser = serial.Serial(port=port.device, baudrate=10000000, write_timeout=1, timeout=0)
            self.requests.clear()
            Log.info(f"Connected to {self.ser.port}")

            # correct_dg = 0
            # avail = 20

            # while True:
            #     wrong = 0
            #     correct = 0
            #     missing_sync = 0
            #     board_wrong = 0
            #     # rr = ReadRequest(13)
            #     b = [0x01,0x04]*(avail)

            #     if(avail > 0):
            #         self.ser.write(bytes(b))
            #         avail = 0

            #     # time.sleep(0.001)
            #     fault_time = 0
            #     while self.ser.in_waiting >= 6:

            #         cmd = self.ser.read(1)
            #         if (cmd[0] & 0x3) != 0x01:
            #             missing_sync+=1
            #             break
            #             continue

            #         resp = ReadResponse(cmd + self.ser.read(5))
            #         # Log.info(f'{resp.addr} -> {resp.value} {resp.sync} {resp.time}')
            #         if (resp.addr != 16 or resp.value != 0x00000000):
            #             # b = [0x00]
            #             # self.ser.write(bytes(b))
            #             if(resp.addr == 5):
            #                 board_wrong +=1
            #             wrong +=1
            #             # Log.info(f'wrong data!')
            #             break
            #         else:
            #             correct +=1
            #             avail +=1

            #         fault_time +=1

            #     if wrong > 0 or missing_sync > 0:
            #         Log.info(f'correct: {correct}, faulty: {wrong}, bwrong: {board_wrong}, missing_sync_bytes: {missing_sync}, correct_dg:{correct_dg}, fault_time:{fault_time}')
            #         correct_dg =0
            #         avail = 5
            #         self.ser.reset_output_buffer()
            #         self.ser.reset_input_buffer()
            #         time.sleep(1)
            #         self.ser.reset_output_buffer()
            #         self.ser.reset_input_buffer()
            #     else:
            #         correct_dg +=1

            # self.async_handle = asyncio.create_task(self.handle())
            self.handle_thread = threading.Thread(target=self.handle, daemon=True)
            self.handle_thread.start()

        except Exception:
            print("could not open serial port")

    def read_reg(self, node):
        Log.debug(f"Read Register: {node.get_name()}")
        self.tx_prio_queue.put(ReadRequest(node.get_offset(-1)))
        # self.requests.add(req.addr, node)
        node.updated.wait()
        Log.debug(f"got value: {node.value}")
        return node.value

    def queue_read_reg(self, node):
        if node is not None:
            # Log.debug(f'Read Register: {node.get_name()}')
            req = ReadRequest(node.address)
            self.read_queue += bytes(req)
            self.requests.add(req.addr, node)

    def queue_execute(self):
        if len(self.read_queue) > 0:
            self.ser.write(self.read_queue)
            self.read_queue = bytes()

    def write_reg(self, node, value):
        Log.debug(f"write_reg: {node.get_name()}, 0x{value:08X}")
        self.tx_prio_queue.put(WriteRequest(node.get_offset(-1), value))

    def handle(self):
        print(f"runnin {self.ser}")
        # goodcnt = 0
        while not self.handle_stop.is_set():
            time.sleep(0.01)
            # self.requests.remove_old_requests(time.time_ns()-1e9)

            recv_pkgs = 0
            # nodes = []
            while self.ser.in_waiting >= 6:
                cmd = self.ser.read(1)
                if (cmd[0] & 0x03) != 0x01:
                    # print(f'got wrong data {cmd[0]} good:{goodcnt}')
                    # goodcnt=0
                    # self.requests.remove_old_requests(time.time_ns()-1e9)
                    continue

                resp = ReadResponse(cmd + self.ser.read(5))
                Log.debug(f"RX: {resp}")
                self.rx_queue.put(resp)
                recv_pkgs += 1

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

            # # while self.ser.out_waiting <= 10 and rx_utilization < 0.1 and self.awaited_responses < 1:
            # while (self.requests.num() < 1000) and (self.requests.num() < (recv_pkgs * 2 + 10)):
            #     node = self.regmon.get_next()
            #     if node is None:
            #         break
            #         pass
            #     else:
            #         self.queue_read_reg(node)
            # self.queue_execute()

            tx_bytes = bytes()
            for i in range(min(1000, recv_pkgs * 2 + 10)):
                if not self.tx_prio_queue.empty():
                    tx_bytes += bytes(self.tx_prio_queue.get())
                    self.tx_prio_queue.task_done()
                elif not self.tx_queue.empty():
                    tx_bytes += bytes(self.tx_queue.get())
                    self.tx_queue.task_done()
                else:
                    break

            if len(tx_bytes) > 0:
                Log.debug(f"Sending bytes: {tx_bytes}")
                self.ser.write(tx_bytes)

    def disconnect(self):
        # self.async_handle.cancel()

        self.handle_stop.set()
        self.handle_thread.join()
        if self.ser is not None:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self.ser.close()
