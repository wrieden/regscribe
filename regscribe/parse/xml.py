import html
import json
import logging
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from enum import Enum

import requests

from regscribe.converter import *

namespaces = {"xsi": "http://www.w3.org/2001/XMLSchema-instance"}


def get_parser():
    return parse_xml()


class parse_xml(Parser):
    def __init__(self, input_filename=None):
        self.input_filename = input_filename

    def get_argparse(self):
        argparser = argparse.ArgumentParser(add_help=False)
        group = argparser.add_argument_group("XML Parser Arguments")
        group.add_argument("-i", "--input", required=True, help="file path of the XML description; can be either relative or absolute path or an URL")
        return argparser

    def set_args(self, args):
        self.input_filename = args.input

    def parse(self):
        with open(self.input_filename, "r", encoding="utf8") as f:
            tree = ET.parse(f)
            root = tree.getroot()

            name = self.get_attribute_value(root, "name")
            offset = self.get_attribute_value(root, "offset", "int", 0)
            address_width = self.get_attribute_value(root, "address_width", "int", None, logging.NOTSET)
            Log.debug(f"Parsing Project: {name}")

            project = Project(name, offset=offset, description="", address_width=address_width)
            project.id = name
            defaults = self.parse_defaults(root)
            project.set_attributes(self.parse_attributes(root))

            for xml_block in root.findall(f"./block"):
                self.parse_block(xml_block, project, defaults)

            project.filter_templates()
            project.calculate_offsets()
            project.simplify_formulas()
            project.add_children_as_attributes()
            project.update_register_addresses()
            project.check_valid()

            return project

    def parse_block(self, xml_node, parent, defaults={}, base: Block = None):
        raw_name = self.get_attribute_value(xml_node, "name", "str", "").strip()
        Log.debug(f"Parsing Address Block: {raw_name}")

        raw_visibility = self.get_attribute_value(xml_node, "visibility", "Visibility", parent.visibility)
        raw_offset = self.get_attribute_value(xml_node, "offset", "int", None, logging.NOTSET)
        raw_instance = self.get_attribute_value(xml_node, ["instance", "inst"], "str", None, logging.NOTSET)
        raw_base_id = self.get_attribute_value(xml_node, "base", "str", None, logging.NOTSET)
        raw_description = self.get_child_or_attribute_value(xml_node, ["description", "desc"], "str", "")
        raw_id = self.get_attribute_value(xml_node, "id", "str", None, logging.NOTSET)
        raw_template = self.get_attribute_value(xml_node, "template", "bool", not(raw_name or raw_instance or raw_base_id))

        defaults = self.parse_defaults(xml_node, defaults)

        node = Block(parent, name=raw_name, description=raw_description, offset=raw_offset, visibility=raw_visibility, id=raw_id, template=raw_template)
        instances = self.parse_instances(raw_instance)
        node.instance = next(iter(instances)) if instances else None
        node.set_attributes(self.parse_attributes(xml_node))

        if base is not None:
            node.set_base(base)
        elif raw_base_id is not None:
            if not node.set_base_by_id(raw_base_id):
                Log.fatal(f"Could not find base node {raw_base_id} of {raw_name}")
            else:
                Log.debug(f"Found instance {node.instance} of {raw_name} ({raw_base_id})")
        else:
            for element in xml_node:
                if element.tag == "block":
                    self.parse_block(element, node, defaults)
                elif element.tag == "register":
                    self.parse_register(element, node, defaults)

        # for inst in instances:
        #     xml_node.set("instance", f"{inst}")
        #     xml_node.set("base", f"{node.id}")
        #     self.parse_block(xml_node, parent, defaults)

        self.handle_instances(xml_node, node, instances, defaults, self.parse_block)

        return node

    def parse_register(self, xml_node, parent, defaults: dict = {}, base:Register = None):

        raw_name = self.get_attribute_value(xml_node, "name", "str", "").strip()
        Log.debug(f"Parsing Register: {raw_name}")

        raw_visibility = self.get_attribute_value(xml_node, "visibility", "Visibility", parent.visibility)
        raw_offset = self.get_attribute_value(xml_node, "offset", "int", None, logging.NOTSET)
        raw_instance = self.get_attribute_value(xml_node, ["instance", "inst"], "str", None, logging.NOTSET)
        raw_id = self.get_attribute_value(xml_node, "id", "str", None, logging.NOTSET)
        raw_base_id = self.get_attribute_value(xml_node, "base", "str", None, logging.NOTSET)
        raw_width = self.get_attribute_value(xml_node, "width", "int", defaults.get("register_width", f"32"))
        raw_description = self.get_child_or_attribute_value(xml_node, ["description", "desc"], "str", "")
        raw_template = self.get_attribute_value(xml_node, "template", "bool", not(raw_name or raw_instance or raw_base_id))

        defaults = self.parse_defaults(xml_node, defaults)

        # instances = []
        # if raw_instance is not None and "," in raw_instance:
        #     instances = raw_instance.split(",")
        #     raw_instance = instances[0]
        #     instances = instances[1:]

        node = Register(parent, name=raw_name, description=raw_description, offset=raw_offset, visibility=raw_visibility, id=raw_id, template=raw_template)
        instances = self.parse_instances(raw_instance)
        node.instance = next(iter(instances)) if instances else None
        node.set_attributes(self.parse_attributes(xml_node))

        if base is not None:
            node.set_base(base)
        elif raw_base_id is not None:
            if not node.set_base_by_id(raw_base_id):
                Log.fatal(f"Could not find base node of {node.get_name()}")
            else:
                Log.debug(f"Found instance {node.instance} of {node.get_name()}")


        else:
            node.width = raw_width
            # node.set_attributes(raw_parameter_custom)

            if not xml_node.findall(f"./field"):
                field_name = self.get_attribute_value(xml_node, "field_name", "str", node.get_name())
                field_width = self.get_attribute_value(xml_node, "field_width", "int", node.width)
                xml_field = ET.SubElement(xml_node, "field", name=field_name, width=field_width, omit="true")
                defaults = self.parse_defaults(
                    xml_node,
                    defaults,
                    {"field": ["access", "logic_access", "on_write_one", "on_write_zero", "on_read", "sign", "reset", "unit", "formula", "desc", "description", "encoding", "status"]},
                )

            for xml_field in xml_node.findall(f"./field"):
                defaults = self.parse_defaults(
                    xml_node,
                    defaults,
                    {"field": ["access", "logic_access", "on_write_one", "on_write_zero", "on_read", "sign", "reset", "unit", "formula", "encoding", "status"]},
                )
                self.parse_field(xml_field, node, defaults)

        self.handle_instances(xml_node, node, instances, defaults, self.parse_register)

        return node

    def parse_field(self, xml_node, parent, defaults: dict = {}, base:Field=None):
        raw_name = self.get_attribute_value(xml_node, "name", "str", "").strip()
        Log.debug(f"Parsing Field: {raw_name}")

        num_choices = 0
        for c in xml_node.findall(f"./choice"):
            i = self.parse_instances(self.get_attribute_value(c, ["instance", "inst"], "str", None, logging.NOTSET))
            num_choices += 1 if not i else len(i)

        raw_visibility = self.get_attribute_value(xml_node, "visibility", "Visibility", parent.visibility)
        raw_omit = self.get_attribute_value(xml_node, "omit", "bool", False)
        raw_offset = self.get_attribute_value(xml_node, "offset", "int", None, logging.NOTSET)
        raw_description = self.get_child_or_attribute_value(xml_node, ["description", "desc"], "str", defaults.get("field_desc", defaults.get("field_description", "")))
        raw_instance = self.get_attribute_value(xml_node, ["instance", "inst"], "str", None, logging.NOTSET)
        raw_id = self.get_attribute_value(xml_node, "id", "str", None, logging.NOTSET)
        raw_base_id = self.get_attribute_value(xml_node, "base", "str", None, logging.NOTSET)
        raw_template = self.get_attribute_value(xml_node, "template", "bool", not(raw_name or raw_instance or raw_base_id))


        node = Field(parent, name=raw_name, description=raw_description, offset=raw_offset, visibility=raw_visibility, omit=raw_omit, id=raw_id, template=raw_template)
        instances = self.parse_instances(raw_instance)
        node.instance = next(iter(instances)) if instances else None
        node.set_attributes(self.parse_attributes(xml_node))

        if base is not None:
            node.set_base(base)
        elif raw_base_id is not None:
            if not node.set_base_by_id(raw_base_id):
                Log.fatal(f"Could not find base node of {node.get_name()}")
            else:
                Log.debug(f"Found instance {node.instance} of {node.get_name()}")

        else:
            node.tags = self.get_child_value(xml_node, "tag", "strset", defaults.get("field_tag", set()))

            node.status = self.get_attribute_value(xml_node, "status", "Status", defaults.get("field_status", Status.IMPLEMENTED))
            node.width = self.get_attribute_value(xml_node, "width", "int", math.ceil(math.log2(num_choices)) if num_choices > 0 else 1)
            node.encoding = self.get_attribute_value(xml_node, "encoding", "Encoding", defaults.get("field_encoding", Encoding.CHOICE if num_choices > 0 else Encoding.UNSIGNED))
            node.exponent = self.get_attribute_value(xml_node, "exponent", "int", 0)
            node.formula = self.get_attribute_value(xml_node, "formula", "str", defaults.get("field_formula", "$"))
            node.unit = self.get_attribute_value(xml_node, "unit", "str", defaults.get("field_unit", ""))
            node.access = self.get_attribute_value(xml_node, "access", "Access", defaults.get("field_access", Access.RW))
            node.clock_signal = self.get_attribute_value(xml_node, "clock_signal", "str", defaults.get("field_clock_signal", "clk"))
            node.reset_signal = self.get_attribute_value(xml_node, "reset_signal", "str", defaults.get("field_reset_signal", "rst_n"))
            node.write_enable = self.get_attribute_value(xml_node, "write_enable", "str", defaults.get("field_write_enable", None), logging.NOTSET)
            node.read_enable = self.get_attribute_value(xml_node, "read_enable", "str", defaults.get("field_read_enable", None), logging.NOTSET)
            node.write_strobe = self.get_attribute_value(xml_node, "write_strobe", "WriteStrobe", defaults.get("field_write_strobe", WriteStrobe.NONE))
            node.read_strobe = self.get_attribute_value(xml_node, "read_strobe", "ReadStrobe", defaults.get("field_read_strobe", ReadStrobe.NONE))

            node.mantissa = self.get_attribute_value(xml_node, "mantissa", "int", node.width)
            node.logic_access = self.get_attribute_value(xml_node, "logic_access", "LogicAccess", defaults.get("field_logic_access", LogicAccess.R if node.access.can_write() else LogicAccess.RW))

            # node.set_attributes(raw_parameter_custom)
            for xml_choice in xml_node.findall(f"./choice"):
                self.parse_choice(xml_choice, node)
            
            node.calculate_offsets()
            min_choice = min([choice.offset for choice in node.children], default=0)
            max_choice = max([choice.offset for choice in node.children], default=(1 << (node.width)) - 1)

            node.min = self.get_attribute_value(xml_node, "min", "int", -(1 << (node.width - 1)) if node.encoding.signed() else min_choice)
            node.max = self.get_attribute_value(xml_node, "max", "int", (1 << (node.width - 1)) - 1 if node.encoding.signed() else max_choice)
            node.reset_value = self.get_attribute_value(xml_node, "reset", "int", defaults.get("field_reset", "0"), logging.FATAL, {"min": node.min, "max": node.max})

        self.handle_instances(xml_node, node, instances, defaults, self.parse_field)

        return node

    def parse_choice(self, xml_node, parent, defaults={}, base:Choice=None):
        raw_name = self.get_attribute_value(xml_node, "name", "str", "").strip()
        Log.debug(f"Parsing Field: {raw_name}")

        raw_visibility = self.get_attribute_value(xml_node, "visibility", "Visibility", parent.visibility)
        raw_offset = self.get_attribute_value(xml_node, "offset", "int", None, logging.NOTSET)
        raw_instance = self.get_attribute_value(xml_node, ["instance", "inst"], "str", None, logging.NOTSET)
        raw_id = self.get_attribute_value(xml_node, "id", "str", None, logging.NOTSET)
        raw_base_id = self.get_attribute_value(xml_node, "base", "str", None, logging.NOTSET)
        raw_description = self.get_child_or_attribute_value(xml_node, ["description", "desc"], "str", "")
        raw_template = self.get_attribute_value(xml_node, "template", "bool", not(raw_name or raw_instance or raw_base_id))


        defaults = self.parse_defaults(xml_node, defaults)

        node = Choice(parent, name=raw_name, description=raw_description, offset=raw_offset, visibility=raw_visibility, id=raw_id, template=raw_template)
        instances = self.parse_instances(raw_instance)
        node.instance = next(iter(instances)) if instances else None
        node.set_attributes(self.parse_attributes(xml_node))

        if base is not None:
            node.set_base(base)
        elif raw_base_id is not None:
            if not node.set_base_by_id(raw_base_id):
                Log.fatal(f"Could not find base node of {node.get_name()}")
            else:
                Log.debug(f"Found instance {node.instance} of {node.get_name()}")

        self.handle_instances(xml_node, node, instances, defaults, self.parse_choice)

        return node

    def get_child_value(self, parent, name, typ="str", default=None, severity=logging.FATAL, lookup=dict()):
        names = name if isinstance(name, list) else [name]
        name = names[0]
        for n in names:
            if parent.find(f"./{n}", namespaces) is not None:
                name = n
                break
        return self.get_xml_value(parent=parent, path=f"./{name}", typ=typ, default=default, severity=severity, lookup=lookup)

    def get_attribute_value(self, parent, name, typ="str", default=None, severity=logging.FATAL, lookup=dict()):
        names = name if isinstance(name, list) else [name]
        name = names[0]
        for n in names:
            if parent.get(n, None) is not None:
                name = n
                break
        return self.get_xml_value(parent=parent, path=".", attribute=name, typ=typ, default=default, severity=severity, lookup=lookup)

    def get_child_or_attribute_value(self, parent, name, typ="str", default=None, severity=logging.FATAL, lookup=dict()):
        names = name if isinstance(name, list) else [name]
        for n in names:
            if parent.get(n, None) is not None:
                return self.get_xml_value(parent=parent, path=".", attribute=n, typ=typ, default=default, severity=severity, lookup=lookup)
            if parent.find(f"./{n}", namespaces) is not None:
                return self.get_xml_value(parent=parent, path=f"./{n}", typ=typ, default=default, severity=severity, lookup=lookup)
        return self.get_xml_value(parent=parent, path=f"./{names[0]}", typ=typ, default=default, severity=severity, lookup=lookup)

    def get_xml_value(self, parent, path, attribute=None, typ="str", default=None, severity=logging.FATAL, lookup=dict()):
        node = parent.find(path, namespaces)

        if attribute is None:
            node_text = node.text if (node is not None) else None
        else:
            node_text = node.get(attribute, None)

        if (node_text is not None) or (default is not None):
            value = node_text if (node_text is not None) else default
            # Log.debug(f'Reading xml value {value} from {path}')

            if isinstance(value, str):
                for key, val in lookup.items():
                    value = re.sub(f"{key}", f"{val}", value)

            if typ == "str":
                return str(value)
            if typ == "strset":
                return set([s.strip() for s in str(value).split(',')])
            elif typ == "int":
                if isinstance(value, str):
                    if value.isdigit():
                        return int(value, base=0)
                    else:
                        return int(eval(value))
                else:
                    return int(value)
            elif typ == "bool":
                return True if str(value).lower() in ["true", "yes", "1"] else False
            elif typ == "Access":
                return Access(value)
            elif typ == "LogicAccess":
                return LogicAccess(value)
            # elif typ == "OnReadType":
            #     return Field.OnReadType(value)
            # elif typ == "OnWriteType":
            #     return Field.OnWriteType(value)
            elif typ == "Encoding":
                return Encoding(value)
            elif typ == "Visibility":
                return Visibility(value)
            elif typ == "WriteStrobe":
                return WriteStrobe(value)
            elif typ == "ReadStrobe":
                return ReadStrobe(value)
            elif typ == "Status":
                value = {"unimp": "unimplemented", "imp": "implemented", "dep": "deprecated",  "rem": "removed",  "exp": "experimental", "undef": "undefined"}.get(value, value)
                return Status(value)
            else:
                Log.fatal(f"Unhandled Type: {typ}")
        else:
            Log.log(severity, f"Missing Node: {path}")

        return None

    def parse_defaults(self, xml_node, defaults: dict = {}, passthrough: dict = {}):
        defaults = copy.deepcopy(defaults)
        for name, value in xml_node.attrib.items():
            if re.match(r"(block|register|field|choice)_", name):
                defaults[name] = value
            else:
                for pre, suf in passthrough.items():
                    if name in suf:
                        defaults[f"{pre}_{name}"] = value
        return defaults

    def parse_attributes(self, xml_node: ET.Element):
        attr = {}
        attr_node = xml_node.find("./attributes")
        if attr_node is not None:
            for name, value in attr_node.items():
                attr[name] = value
        return attr

    def parse_instances(self, instances: str):
        if instances in [None, ""]:
            return {}
        inst_arr = {}
        for inst in instances.split(","):
            if array := re.match(r"\s*(\d*.\d*)\s*;\s*(\d*.\d*)\s*;\s*(\d*.\d*)\s*", inst):
                # i = float(array.group(1))
                # while i < float(array.group(2)):
                for i in range(int(array.group(1)), int(array.group(2)), int(array.group(3))):
                    inst_arr[f"{i}"] = {}
                    # i += float(array.group(3))
            else:
                parts = inst.split(":")
                inst_arr[parts[0]] = {}
                for part in parts[1:]:
                    part = part.split("=", 1)
                    inst_arr[parts[0]][part[0]] = part[1]
        return inst_arr

    def handle_instances(self, xml_node, node, instances, defaults, func):
        for inst, args in list(instances.items())[1:]:
            xml_inst = copy.deepcopy(xml_node)
            xml_inst.set("instance", f"{inst}")
            # xml_inst.set("base", f"{node.id}")
            xml_inst.attrib.pop("offset", None)
            for key, val in args.items():
                xml_inst.set(key, val)
            func(xml_inst, node.parent, defaults, base=node)
