import argparse
import logging
import os
import pathlib
import re
import uvicorn
from datetime import datetime
from pathlib import Path
from regscribe.comm.uart import comm_uart

from regscribe.converter import *
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi_utils.tasks import repeat_every
from fastapi import Response, status, Request
from sse_starlette.sse import EventSourceResponse
import asyncio
import math
import time
from regscribe.converter import Log
import json
import random
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager



# Create local logger
logger = logging.getLogger(__name__)


def get_composer():
    return compose_server()


class compose_server(Composer):
    def __init__(self):
        self.output_filename = None

    def get_argparse(self):
        argparser = argparse.ArgumentParser(add_help=False)
        group = argparser.add_argument_group("Server Arguments")
        # group.add_argument("--include_defaults", action="store_true", help="Do not optimize the default values out")
        # group.add_argument("-o", "--output", type=Path, default="out.xml", help="Output xml file")
        return argparser

    def set_args(self, args):
        # self.output_filename: Path = args.output
        pass

    def compose(self, project: Project):
        # create comm and connect (starts background threads)
        comm = comm_uart(project)
        comm.connect(False)

        # lifespan handlers to ensure comm is closed on shutdown
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            Log.info('Startup')
            yield
            Log.info('Shutdown')
            try:
                comm.disconnect()
            except Exception:
                pass

        app = FastAPI(lifespan=lifespan)

        origins = ["http://localhost:3000", "localhost:3000"]

        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"]
        )

        # # serve client files if present
        # client_dir = Path("client")
        # if client_dir.exists() and client_dir.is_dir():
        #     app.mount('/', StaticFiles(directory=str(client_dir), html=True), name="static")

        # define routes that capture comm and project
        @app.get("/py/monitor_register")
        async def monitor_register(id: str = "", prio: int = 1, endpoint: str = "default"):
            node: Register = project.get_child(id)
            if not isinstance(node, Register):
                return Response(content="id not found", status_code=status.HTTP_404_NOT_FOUND)
            comm.regmon.add_listener(node, endpoint, prio)
            Log.debug(comm.regmon.monitored)
            return Response(status_code=status.HTTP_200_OK)

        @app.get("/py/write_register")
        async def write_register(id: str = "", value: int = 0):
            node: Register = project.get_child(id)
            if not isinstance(node, Register):
                return Response(content="id not found", status_code=status.HTTP_404_NOT_FOUND)
            Log.info(f'Write Register: {node.get_name()} -> {value}')
            await asyncio.to_thread(comm.write_reg, node, value)
            await asyncio.to_thread(comm.read_reg, node)
            return Response(status_code=status.HTTP_200_OK)

        @app.get("/py/read_register")
        async def read_register(id: str = ""):
            node:Register = project.get_child(id)
            if not isinstance(node, Register):
                return Response(content="id not found", status_code=status.HTTP_404_NOT_FOUND)
            Log.info(f'Read Register: {node.get_name()}')
            # run blocking read in thread
            await asyncio.to_thread(comm.read_reg, node)
            return Response(status_code=status.HTTP_200_OK)

        @app.get("/py/get_project")
        async def get_project():
            Log.info('Read Project')
            return project.to_dict()

        @app.get("/py/stream_updates")
        async def stream_updates(request: Request):
            async def generator():
                while True:
                    if await request.is_disconnected():
                        break
                    if comm.updates.updates:
                        yield f'{json.dumps(comm.updates.to_dict())}\n'
                        comm.updates.clear()

                    await asyncio.sleep(0.01)
            return StreamingResponse(generator(), media_type='application/x-ndjson')

        # Run uvicorn with the app instance. Do not enable auto-reload here.
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level='info')









    # @asynccontextmanager
    # async def lifespan(app: FastAPI):
    #     Log.info('Startup')
    #     loop = asyncio.get_running_loop()
    #     loop.create_task(comm.handle())
    #     yield
    #     Log.info('Shutdown')
    #     comm.close()





    # @app.get("/py/monitor_register")
    # async def read_item(id: str = "", prio: int = 1, endpoint: str = "default"):
    #     node = project.get_child(id)
    #     if node is None: return "id not found"
    #     comm.regmon.add_listener(node, endpoint, prio)
    #     Log.debug(comm.regmon.monitored)
    #     return Response(status_code=status.HTTP_200_OK)

    # @app.get("/py/write_register")
    # async def write_register(id: str = "", value: int = 0):
    #     node = project.get_child(id)
    #     if node is None: return "id not found"
    #     Log.info(f'Write Register: {node.get_name()} -> {value}')
    #     comm.write_reg(node, value)
    #     comm.read_reg(node)

    #     return Response(status_code=status.HTTP_200_OK)


