import argparse
import html
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import reduce

import requests

# import xml.etree.ElementTree as ET
from lxml import etree as ET

from regscribe.converter import *

# Create local logger
logger = logging.getLogger(__name__)


def get_composer():
    return compose_python_tree()


class compose_python_tree(Composer):
    def __init__(self, output_filename: Path):
        self.output_filename = output_filename

    def get_argparse(self):
        argparser = argparse.ArgumentParser(add_help=False)
        group = argparser.add_argument_group("XML Builder Arguments")
        group.add_argument("-o", "--output", type=Path, default="out.py", help="Output python file")
        return argparser

    def set_args(self, args):
        self.output_filename: Path = args.output

    class text_helper(object):
        def __init__(self):
            self.text_pre = ""
            self.text = []
            self.text_post = ""

        def add_pre(self, text):
            self.text_pre += f"{text}\n"

        def add_post(self, text):
            self.text_post += f"{text}\n"

        def add_child(self, child):
            self.text.append(child)

        def get(self, indent=""):
            text = "" if self.text_pre == "" else re.sub(r"^.", f"{indent}\g<0>", self.text_pre, flags=re.MULTILINE)

            for child in self.text:
                text += child.get(f"{indent}    ")

            text += "" if self.text_post == "" else re.sub(r"^.", f"{indent}\g<0>", self.text_post, flags=re.MULTILINE)
            return text

        def write_to_file(self, name, indent="    "):
            Path(Path(name).parents[0]).mkdir(parents=True, exist_ok=True)

            with open(name, "w") as f:
                f.write(self.get())

    def compose(self, project):
        logger.info(f"Creating Python Tree file")

        self.project = project

        self.default_register_offset = 0
        self.default_field_offset = 0
        self.default_choice_offset = 0

        struct_text = dict()
        struct_text = self.text_helper()
        struct_text.add_pre(f"from __future__ import annotations")
        struct_text.add_pre(f"from regscribe.converter import Project, Block, Register, Field, Choice")
        self.eval_node(self.project, struct_text)

        # self.output_filename.parents[0].mkdir(parents=True, exist_ok=True)
        struct_text.write_to_file(self.output_filename)

   

    def set_attribute(self, parent, name, value, default=None):
        if f"{value}" != f"{default}":
            parent.set(name, f"{value}")

    def set_element(self, parent, name, value, default):
        if f"{value}" != f"{default}":
            ET.SubElement(parent, f"{name}").text = f"{value}"

    def set_node_attributes(self, parent, attributes):
        if attributes:
            xml_attributes = ET.SubElement(parent, "attributes")
            for attr_name, attr_value in attributes.items():
                self.set_attribute(xml_attributes, f"{attr_name}", f"{attr_value}")

    def eval_node(self, node : BaseNode, struct_text):
        Log.debug(f"Writing Node: {node.name}")

        if True: #not node.exclude:

            inst_type = f"{node.__class__.__name__}"
            doc = f'"""\n**{node.get_hier_name().replace('/', ' -> ')}**:\n\n    Type: {node.get_type_name()}\n'

            if isinstance(node, Register):
                doc += f"    address: 0x{node.get_offset(-1):02X}\n    width: {node.width}"

            if isinstance(node, Field):
                doc += f"    offset: 0x{node.get_offset():02X}\n    width: {node.width}"
                doc += f"    reset: 0x{node.reset_value:02X}\n    access: {node.access}"

            doc += '"""'

            if node.get_children():
                inst_type = f"t{node.get_name()}"
                base_type = f"t{node.get_base().get_name_without_instance()}"

                # if not node.is_instance():
                struct_text.add_pre(f"class {inst_type}({node.__class__.__name__}):")

                for child in node.get_children(): # + node.excluded
                    child_text = self.text_helper()
                    self.eval_node(child, child_text)
                    struct_text.add_child(child_text)

                # struct_text.add_post(f'    pass')

            if node.get_instance_index_in_parent() == 0:
                if len(node.get_possible_array_member()) > 1:
                    struct_text.add_post(f"{node.get_base().get_name_without_instance()} : list[{inst_type}] = []")
                    struct_text.add_post(doc)

            struct_text.add_post(f"{node.get_name()} : {inst_type} = None")
            struct_text.add_post(doc)

        if isinstance(node, Project):
            pass

        elif isinstance(node, Block):
            pass

        elif isinstance(node, Register):
            pass

        elif isinstance(node, Field):
            pass

        elif isinstance(node, Choice):
            pass
