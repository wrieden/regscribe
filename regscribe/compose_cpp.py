import argparse
import logging
import os
import pathlib
import re
from datetime import datetime
from pathlib import Path
from lxml import etree as ET

from regscribe.converter import *

# Create local logger
logger = logging.getLogger(__name__)


def get_composer():
    return compose_cpp()


class compose_cpp(Composer):
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

        def write_to_file(self, path: Path, indent="    "):
            path.parents[0].mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                f.write(self.get())

    def compose(self, project):
        logger.info(f"Creating CPP file")

        self.project = project

        self.default_register_offset = 0
        self.default_field_offset = 0
        self.default_choice_offset = 0

        struct_text = dict()
        struct_text = self.text_helper()
        struct_text.add_pre(f"#pragma once")
        struct_text.add_pre(f"#include <stdint.h>")

        self.eval_node(self.project, struct_text)

        regs = self.project.get_children(-1, Register)
        reg_reset_parent = self.text_helper()
        # reg_reset_parent.add_pre(f'void init_regmap(uint32_t *regs){{')
        reg_reset_parent.add_pre(f"static const uint32_t reset_values[{regs[-1].get_offset(-1)+1}] = {{")
        reg_reset_parent.add_post(f"}};")
        reg_reset = self.text_helper()
        for reg in regs:
            # reg_reset.add_pre(f'regs[0x{reg.get_offset(-1):04X}] = 0x{reg.reset_value:08X}; //{reg.get_hier_name()}')
            reg_reset.add_pre(f'0x{reg.reset_value:08X}{" " if reg == regs[-1] else ","} // 0x{reg.get_offset(-1):04X}: {reg.get_hier_name()}')
        reg_reset_parent.add_child(reg_reset)

        struct_text.add_post(reg_reset_parent.get())
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

    def eval_node(self, node, struct_text):
        Log.debug(f"Writing Node: {node.name}")

        if isinstance(node, Project):

            struct_text.add_pre(f"typedef struct __attribute__((aligned(4))) {{")

            for child in node.templates + node.get_children():
                child_text = self.text_helper()
                self.eval_node(child, child_text)
                struct_text.add_child(child_text)

            struct_text.add_post(f"}} t{node.get_name_without_instance()};")

        elif isinstance(node, Block):
            if not node.is_instance():
                struct_text.add_pre(f"typedef struct {{")

                for child in node.get_children():
                    child_text = self.text_helper()
                    self.eval_node(child, child_text)
                    struct_text.add_child(child_text)

                struct_text.add_post(f"}} t{node.get_name_without_instance()};")

            if not node.template:
                if node.get_instance_index_in_parent() == 0:
                    if node.parent != node.get_base().parent:
                        typename = node.get_base().get_hier_name_without_instance()
                        typename = typename[len(os.path.commonprefix([typename, node.get_hier_name_without_instance()])) :]
                        typename = re.sub(r"(\w+)", f"t\g<0>", typename)
                        typename = re.sub(r"/", f"::", typename)
                        basename = f"t{node.get_base().get_name_without_instance()}"
                        if typename != basename:
                            struct_text.add_post(f"typedef {typename} t{node.get_base().get_name_without_instance()};")

                    member = node.get_possible_array_member()

                    if len(member) > 1:
                        struct_text.add_post(f"union {{")
                        struct_text.add_post(f"   struct {{")
                        for m in member:
                            struct_text.add_post(f"      t{m.get_base().get_name_without_instance()} {m.get_name()};")
                        struct_text.add_post(f"   }};")
                        struct_text.add_post(f"   t{node.get_base().get_name_without_instance()} {node.get_base().get_name_without_instance()}[{len(member)}];")
                        struct_text.add_post(f"}};")
                    else:
                        struct_text.add_post(f"t{node.get_base().get_name_without_instance()} {node.get_name()};")
                else:
                    prev = node
                    prev_old = node
                    while prev is not None and prev.get_base() == node.get_base():
                        prev_old = prev
                        prev = prev.get_previous_child_node()
                    prev = prev_old

                    if prev is None or prev.get_possible_array_size() == 1:
                        struct_text.add_post(f"t{node.get_base().get_name_without_instance()} {node.get_name()};")

        elif isinstance(node, Register):
            if (
                (len(node.get_children()) == 1)
                and (node.get_children()[0].omit)
                and (node.get_children()[0].width in [8, 16, 32])
                and (node.get_children()[0].get_offset() == 0)
            ):
                if not node.template:
                    field : Field = node.get_children()[0]
                    # if field.get_offset() != 0:
                    #     struct_text.add_pre(f'uint32_t _reserved_prealign_{node.get_name()} : {field.get_offset()} = 0;')
                    # struct_text.add_pre(f'{"" if field.encode_sign else "u"}{["int8_t", "int16_t", "","int32_t"][(field.width>>3)-1]} {node.get_name()} = {field.reset_value}; // {node.get_description()}')
                    struct_text.add_pre(
                        f'{"" if field.encoding.signed() else "u"}{["int8_t", "int16_t", "","int32_t"][(field.width>>3)-1]} {node.get_name()}; // {node.get_description()}'
                    )
                    if field.width != 32:
                        struct_text.add_pre(f"uint16_t _reserved_align16_{node.get_name()};")
                    if field.width == 8:
                        struct_text.add_pre(f"uint8_t _reserved_align8_{node.get_name()};")

            else:
                if not node.is_instance():
                    struct_text.add_pre(f"typedef struct __attribute__((packed, may_alias)) {{")
                    self.default_field_offset = 0
                    self.field_cnt = 0

                    for child in node.get_children():
                        child_text = self.text_helper()
                        self.eval_node(child, child_text)
                        struct_text.add_child(child_text)

                    if self.default_field_offset != node.width:
                        child_text = self.text_helper()
                        child_text.add_pre(f"uint32_t _reserved_{self.field_cnt} : {node.width - self.default_field_offset};")
                        self.field_cnt += 1
                        struct_text.add_child(child_text)

                    struct_text.add_post(f"}} t{node.get_name_without_instance()};")
                if not node.template:
                    struct_text.add_post(f"t{node.get_name_without_instance()} {node.get_name()};")

        elif isinstance(node, Field):

            if not node.template and (node.get_offset() != self.default_field_offset):
                struct_text.add_pre(f"uint32_t _reserved_{self.field_cnt} : {node.get_offset() - self.default_field_offset};")
                self.field_cnt += 1

            if node.has_children():
                enum_name = f"t{node.get_name_without_instance()}"
                if not node.is_instance():
                    struct_text.add_pre(f"enum class {enum_name} : uint32_t {{")
                    child_text = self.text_helper()
                    for child in node.get_children():
                        self.eval_node(child, child_text)
                    struct_text.text_pre += child_text.get("    ")
                    struct_text.add_pre(f"}};")

                reset_choice = node.get_child_by_offset(node.reset_value)
                # struct_text.add_pre(f'{enum_name} {node.get_name()} : {node.width} = {enum_name}::{reset_choice.get_name()}; // {node.get_description()}')
                if not node.template:
                    struct_text.add_pre(f"{enum_name} {node.get_name()} : {node.width}; // {node.get_description()}")
            else:
                # struct_text.add_pre(f'{"int32_t" if node.encode_sign else "uint32_t"} {node.get_name()} : {node.width} = {node.reset_value}; // {node.get_description()}')
                if not node.template:
                    struct_text.add_pre(f'{"int32_t" if node.encoding.signed() else "uint32_t"} {node.get_name()} : {node.width}; // {node.get_description()}')
            if not node.template:
                self.default_field_offset = node.get_offset() + node.width

                # child_text = self.text_helper()
                # self.eval_node(child, child_text)
                # struct_text.text = child_text
                # struct_text.add()

                # self.eval_node(child, struct_text)

        elif isinstance(node, Choice):
            if not node.template:
                struct_text.add_pre(f"{node.get_name()} = {node.get_offset()},")
