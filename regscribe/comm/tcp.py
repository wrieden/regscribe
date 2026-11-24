import socket
import threading
import time

from regscribe.converter import Log, Project
from regscribe.comm.comm import comm


class comm_tcp(comm):
    """Register access through a TCP bridge that speaks the datagram specs, one frame per request."""

    def __init__(self, project: Project, host: str, port=5555, ReadReq: type | str = "0A[0:6]0[32]", WriteReq: type | str = "1A[0:6]D[0:31]", ReadResp: type | str = "1A[0:6]D[0:31]"):
        super().__init__(project, ReadReq=ReadReq, WriteReq=WriteReq, ReadResp=ReadResp)

        self.host = host
        self.port = port
        self.sock: socket.socket | None = None

        self.rx_run = True
        self.tx_run = True
        self.handle_tx_thread = None
        self.handle_rx_thread = None

        # reads that may be unanswered at once, the bridge answers in order so nothing is lost
        self.max_inflight = 64
        self.max_tx_batch = 64

    def connect(self, block=False, timeout=5.0):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

        deadline = time.monotonic() + timeout
        while self.sock is None:
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
            except OSError as error:
                if not block or time.monotonic() >= deadline:
                    Log.error(f"Cannot connect to {self.host}:{self.port}: {error}")
                    return
                time.sleep(0.5)

        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(0.05)
        self.requests.clear()
        Log.info(f"Connected to {self.host}:{self.port}")

        self.rx_run = True
        self.tx_run = True
        self.start_response()
        self.handle_rx_thread = threading.Thread(target=self.handle_rx, daemon=True)
        self.handle_tx_thread = threading.Thread(target=self.handle_tx, daemon=True)
        self.handle_rx_thread.start()
        self.handle_tx_thread.start()

    def disconnect(self):
        Log.info("Stopping TCP handler threads")
        self.rx_run = False
        self.tx_run = False
        if self.handle_rx_thread is not None:
            self.handle_rx_thread.join()
            self.handle_rx_thread = None
        if self.handle_tx_thread is not None:
            self.handle_tx_thread.join()
            self.handle_tx_thread = None
        self.stop_response()
        if self.sock is not None:
            Log.info("Closing socket")
            self.sock.close()
            self.sock = None

    def handle_rx(self):
        Log.info("Started handle_rx thread")
        rx_bytearray = bytearray()
        resp_bytes = len(bytes(self.ReadResponse(bytes(64))))

        while self.rx_run:
            try:
                data = self.sock.recv(65536)
            except TimeoutError:
                continue
            except OSError as error:
                Log.error(f"Socket receive failed: {error}")
                break
            if not data:
                Log.warn("Bridge closed the connection")
                break
            rx_bytearray.extend(data)

            while len(rx_bytearray) >= resp_bytes:
                resp = self.ReadResponse(rx_bytearray[:resp_bytes])
                if resp.valid():
                    del rx_bytearray[:resp_bytes]
                    self.resp_queue.put(resp)
                else:
                    Log.warn(f"got wrong data {resp}")
                    del rx_bytearray[0]
        Log.info("Ending tcp rx handler thread")

    def handle_tx(self):
        Log.info("Started handle_tx thread")

        while self.tx_run:
            tx_bytes = []
            for _ in range(min(self.max_tx_batch, self.max_inflight - self.requests.open_requests())):
                if not self.req_queue.empty():
                    req = self.req_queue.get()
                    self.requests.add_request(req)
                else:
                    node = self.regmon.get_next()
                    if node is None:
                        break
                    req = self.ReadRequest(node.address)
                    self.requests.add_request(req)
                if Log.debug_enabled():
                    Log.debug(f"TX: {req}")
                tx_bytes.append(bytes(req))

            if tx_bytes:
                self.sock.sendall(b"".join(tx_bytes))
            else:
                time.sleep(0.0005)
        Log.info("Ending tcp tx handler thread")
