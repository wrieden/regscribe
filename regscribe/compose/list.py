import argparse
import logging
import os
import pathlib
import re
from datetime import datetime
from pathlib import Path
from lxml import etree as ET
from enum import Enum, auto

from matplotlib import container
from regscribe.converter import *

# Create local logger
logger = logging.getLogger(__name__)

def get_composer():
    return compose_list()


class compose_list(Composer):
    def __init__(self):
        pass

    def get_argparse(self):
        argparser = argparse.ArgumentParser(add_help=False)
        group = argparser.add_argument_group("List Arguments")
        group.add_argument("-o", "--output", type=Path, default="out.txt", help="Output base filename, will generate a list of register addresses and values")
        return argparser

    def set_args(self, args):
        self.output: Path = args.output
        
    def compose(self, project: Project):
        logger.info(f"Creating register list file")

        self.project = project

        self.registers: list[Register] = self.project.get_children(-1, Register)
        self.fields: list[Field] = self.project.get_children(-1, Field)
        self.regwidth = self.registers[0].width

        reg_list = ""
        for reg in self.registers:
            mask = 0
            for field in reg.get_children():
                field:Field
                if field.access.can_write() and field.access== Access.RW:
                    mask |= field.mask 
            reg_list += f"{reg.address} 16'h{mask:04X} //{reg.get_name()}\n"

        with open(self.output, 'w', encoding="utf-8") as f:
            f.write(reg_list)
