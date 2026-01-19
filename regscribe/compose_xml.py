import argparse
import html
import json
import logging
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import reduce
from xml.dom import minidom
from enum import Enum
from pathlib import Path

import requests
from regscribe.converter import *

# import xml.etree.ElementTree as ET
from lxml import etree as ET

# Create local logger
logger = logging.getLogger(__name__)


def get_composer():
    return compose_xml()


class compose_xml(Composer):
    def __init__(self):
        pass

    def get_argparse(self):
        argparser = argparse.ArgumentParser(add_help=False)
        group = argparser.add_argument_group("XML Builder Arguments")
        group.add_argument("--include_defaults", action="store_true", help="Do not optimize the default values out")
        group.add_argument("-o", "--output", type=Path, default="out.xml", help="Output xml file")
        return argparser

    def set_args(self, args):
        self.output_filename: Path = args.output

    def compose(self, project):
        logger.info(f"Creating XML file")

        self.project = project

        nsmap = {"xsi": "http://www.w3.org/2001/XMLSchema-instance"}
        ET.register_namespace("xsi", nsmap["xsi"])  # some name

        root = ET.Element("project", name=project.get_name(), nsmap=nsmap)
        root.set(f'{{{nsmap["xsi"]}}}noNamespaceSchemaLocation', "../sample_xml/schema.xsd")

        ET.SubElement(root, "desc").text = project.description.replace("_", " ")

        self.default_register_offset = 0
        self.default_field_offset = 0
        self.default_choice_offset = 0

        self.eval_node(self.project, root)

        try:
            xmlstr = minidom.parseString(ET.tostring(root)).toprettyxml(indent="    ", newl="\n", encoding="utf-8")
        except Exception as e:
            Log.debug(ET.tostring(root))
            Log.fatal(e)
        # xmlstr = re.sub(r'&quot;', '\\"', xmlstr)
        # xmlstr = re.sub(r'&amp;', '\\&', xmlstr)
        # xmlstr = re.sub(r'&gt;', '\\>', xmlstr)
        # xmlstr = re.sub(r'&lt;', '\\<', xmlstr)

        self.output_filename.parents[0].mkdir(parents=True, exist_ok=True)
        with open(self.output_filename, "wb") as f:
            f.write(xmlstr)

    def check_and_convert(self, name, value, default):
        if type(default) != list:
            default = [default]

        name = f"{name}"
        value = f"{value.value}" if isinstance(value, Enum) else f"{value}"
        for d in default:
            d = f"{d.value}" if isinstance(d, Enum) else f"{d}"
            if value == d:
                return True, name, value
        return False, name, value

    def set_attribute_element_by_len(self, parent, name, value, default=[], length=20):
        if len(value) > length:
            self.set_element(parent, name, value, default)
        else:
            self.set_attribute(parent, name, value, default)

    def set_attribute(self, parent, name, value, default=[]):
        # if type(default) != list:
        #     default = [default]
        # name = f"{name}"
        # value = f"{value.value}" if issubclass(value, Enum) else f"{value}"
        # for d in default:
        #     d = f"{d.value}" if issubclass(d, Enum) else f"{d}"
        #     if value == d:
        #         return
        is_default, name, value = self.check_and_convert(name, value, default)
        if not is_default:
            parent.set(name, value)

    def set_element(self, parent, name, value, default=[]):
        is_default, name, value = self.check_and_convert(name, value, default)
        if not is_default:
            ET.SubElement(parent, f"{name}").text = f"{value}"

    def set_node_attributes(self, parent, attributes):
        if attributes:
            xml_attributes = ET.SubElement(parent, "attributes")
            for attr_name, attr_value in attributes.items():
                self.set_attribute(xml_attributes, f"{attr_name}", f"{attr_value}")

    def check_common_attributes(self, xml_node: ET.Element, element_name):
        xml_childs = xml_node.findall(f"./{element_name}")

        if len(xml_childs) > 1:
            common_attributes = dict(xml_childs[0].attrib)
            for xml_child in xml_childs:
                for key, val in dict(common_attributes).items():
                    if xml_child.attrib.get(key) != val:
                        common_attributes.pop(key, None)

            for key, val in common_attributes.items():
                attr_name = key if re.match(r"^(block|register|field|choice)_", key) else f"{element_name}_{key}"
                self.set_attribute(xml_node, attr_name, val)
                for xml_child in xml_childs:
                    xml_child.attrib.pop(key, None)

    def eval_node(self, node, xml_node):
        for child in node.get_children():
            Log.debug(f"Writing Node: {child.name}")
            prev_node = child.get_previous_child_node()
            default_block_offset = 0 if prev_node is None else prev_node.get_offset()
            default_register_offset = 0 if prev_node is None else prev_node.get_offset() + 1
            offset = child.get_offset()

            if isinstance(child, Block):
                xml_child = ET.SubElement(xml_node, "block")

                if child.is_instance():
                    self.set_attribute(xml_child, "base", child.get_base().id)
                else:
                    self.set_attribute(xml_child, "name", child.name)
                    self.set_node_attributes(xml_child, child.get_attributes())
                    self.eval_node(child, xml_child)

                self.set_attribute(xml_child, "instance", child.instance, None)
                self.set_attribute(xml_child, "offset", offset, default_block_offset)
                self.set_attribute(xml_child, "id", child.id, child.get_hier_name())
                self.set_attribute(xml_child, "visibility", child.visibility, Visibility.PUBLIC)

                self.check_common_attributes(xml_child, "register")

            elif isinstance(child, Register):
                # addr = register.hw_address

                xml_child = ET.SubElement(xml_node, "register", name=child.get_name())
                self.set_attribute(xml_child, "visibility", child.visibility, Visibility.PUBLIC)
                self.set_attribute(xml_child, "offset", offset, default_register_offset)
                self.set_attribute(xml_child, "width", child.width, 32)
                self.set_node_attributes(xml_child, child.get_attributes())
                self.set_attribute(xml_child, "id", child.id, child.get_hier_name())

                self.default_field_offset = 0

                field: Field = None
                if len(child.get_children(depth=2)) == 1:
                    field = child.children[0]

                if (
                    field is not None
                    and field.reset_value == child.reset_value
                    and ("" in [field.get_description(), child.get_description()] or field.get_description() == child.get_description())
                    and field.offset == 0
                    and not field.get_attributes()
                ):
                    self.set_attribute(xml_child, "reset", field.reset_value, 0)
                    self.set_attribute(xml_child, "field_width", field.width, child.width)
                    self.set_attribute(xml_child, "field_access", field.access, Access.RW)
                    self.set_attribute(xml_child, "field_encoding", field.encoding, Encoding.CHOICE if field.get_children() else Encoding.UNSIGNED)
                    self.set_attribute_element_by_len(xml_child, "desc", field.get_description_html(), "", 100)

                else:
                    self.set_attribute_element_by_len(xml_child, "desc", child.get_description_html(), "", 100)
                    self.eval_node(child, xml_child)

                self.check_common_attributes(xml_child, "field")

            elif isinstance(child, Field):
                default_width = math.ceil(math.log2(len(child.children))) if child.children else 1

                val_range = 1 << child.width
                default_min = 0
                default_max = val_range - 1
                if child.encoding.signed():
                    default_min = -(val_range >> 1)
                    default_max = (val_range >> 1) - 1

                xml_child = ET.SubElement(xml_node, "field", name=child.get_name())
                self.set_attribute(xml_child, "visibility", child.visibility, Visibility.PUBLIC)
                self.set_attribute(xml_child, "offset", offset, self.default_field_offset)
                self.set_attribute(xml_child, "width", child.width, default_width)
                self.set_attribute(xml_child, "access", child.access, Access.RW)
                self.set_attribute(xml_child, "reset", child.reset_value, 0)
                self.set_attribute(xml_child, "encoding", child.encoding, Encoding.CHOICE if child.get_children() else Encoding.UNSIGNED)
                self.set_attribute(xml_child, "min", child.min, [None, default_min])
                self.set_attribute(xml_child, "max", child.max, [None, default_max])

                self.set_attribute(xml_child, "reset_signal", child.reset_signal, ["rst_n"])
                self.set_attribute(xml_child, "logic_access", child.logic_access, LogicAccess.R if child.access.can_write() else LogicAccess.RW)
                self.set_attribute(xml_child, "read_enable", child.read_enable, [False, ""])
                self.set_attribute(xml_child, "write_enable", child.write_enable, [False, ""])
                self.set_attribute(xml_child, "read_strobe", child.read_strobe, ReadStrobe.NONE)
                self.set_attribute(xml_child, "write_strobe", child.write_strobe, WriteStrobe.NONE)

                # xml_child2 = ET.SubElement(xml_child, 'desc2', value=child.get_description())
                self.set_attribute_element_by_len(xml_child, "desc", child.get_description_html(), "", 100)
                self.set_node_attributes(xml_child, child.get_attributes())

                self.set_attribute(xml_child, "id", child.id, child.get_hier_name())

                self.default_field_offset = offset + child.width
                self.default_choice_offset = 0
                self.eval_node(child, xml_child)
                self.check_common_attributes(xml_child, "choice")

            elif isinstance(child, Choice):
                xml_child = ET.SubElement(xml_node, "choice", name=child.get_name())
                self.set_attribute(xml_child, "value", child.get_offset(), self.default_choice_offset)
                self.set_node_attributes(xml_child, child.get_attributes())
                self.set_attribute(xml_child, "desc", child.get_description_html(), "")

                self.default_choice_offset = self.default_choice_offset + 1
