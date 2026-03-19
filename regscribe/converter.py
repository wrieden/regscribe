import argparse
from ast import Set
import copy
import html
import importlib
import json
import logging
import os
import re
import sys
from enum import Enum
from functools import partial
from threading import Event
from typing import Optional
from glob import glob
import dill as pickle
from pathlib import Path

import math
from math import log, log10, log2, ceil, floor

import IPython.display
from attr import field
import latexify
import requests
import sympy

from IPython.display import Markdown

import IPython
import tabulate

__version__ = "0.1"
class Visibility(str, Enum):
    PUBLIC = "public"  # visible to everyone
    INTERNAL = "internal"  # visible to internal ppl, eg software
    PRIVATE = "private"  # visible to only intended ppl
    HIDDEN = "hidden"  # visible to nobody
class Status(str, Enum):
    IMPLEMENTED = "implemented" # default status, fully supported
    EXPERIMENTAL = "experimental" # new feature, use with caution
    UNIMPLEMENTED = "unimplemented" # not yet implemented
    UNDEFINED = "undefined" # definition missing
    DEPRECATED = "deprecated" # not recommended for use, will be removed in future
    REMOVED = "removed" # removed feature, should not be used

class Access(str, Enum):
    # When adding new modes follow the sheme:
    # R        if read is permitted
    # 0,1,I,U  what is read, eg 1 to always read back 1, ommit if reg val is read
    # C,S,T    how the register is affected, eg C will clear the register after a read
    # W        if write is permitted
    # 0,1      what is written
    # C,S,T    how the register is affected, eg C will clear the register regardless of the written value
    NONE = ""
    R = "r"
    RW = "rw"
    RW1C = "rw1c"
    RW1S = "rw1s"
    RW1T = "rw1t"
    W = "w"
    WC = "wc"
    WS = "ws"
    WT = "wt"
    W0 = "w0"
    W1 = "w1"
    W1C = "w1c"
    W1S = "w1s"
    W1T = "w1t"

    def can_read(self):
        return "r" in self.value

    def can_write(self):
        return "w" in self.value

    def read_only(self):
        return self.can_read() and not self.can_write()

    def write_only(self):
        return not self.can_read() and self.can_write()

    def read_write(self):
        return self.can_read() and self.can_write()

    def read_access(self):
        return Access(re.sub(r"w.*", "", self.value))

    def write_access(self):
        return Access(re.sub(r"r[^w]*", "", self.value))
        
    def write_if(self):
        acc = self.write_access()
        if '0' in acc.value:
            return Access.W0
        elif '1' in acc.value:
            return Access.W1
        else:
            return Access.W

    def write_value(self):
        acc = self.write_access()
        if 'c' in acc.value:
            return Access.WC
        elif 's' in acc.value:
            return Access.WS
        elif 't' in acc.value:
            return Access.WT
        else:
            return Access.W


    # def set_read_modifier(self, modifier: "Access"):
    #     return Access(re.sub(r"r[^w]*", modifier.read_access(), "r" + self.value))

    # def set_write_modifier(self, modifier: "Access"):
    #     return Access(re.sub(r"w.*", modifier.write_access(), self.value + "w"))

    def set_modifier(self, modifier: "Access"):
        acc = self.value

        if "r" in modifier.value:
            if "r" in acc:
                acc = re.sub(r"r[^w]*", modifier.read_access(), self.value)
            else:
                Log.fatal("Trying to add a read modifier to a non read value!")

        if "w" in modifier.value:
            if "w" in acc:
                acc = re.sub(r"w.*", modifier.write_access(), self.value)
            else:
                Log.fatal("Trying to add a read modifier to a non read value!")

        return Access(acc)


class LogicAccess(str, Enum):
    # When adding new modes follow the sheme:
    # R,S,C,U  type of access (Read, Set, Clear, Update)
    # A        modifier (Asynchronous)
    # priority is determined by order of letters, eg for SC, set takes priority over clear, for CS clear takes priority over set

    NONE = ""  # no internal access...
    R = "r"  # only read access

    W = "w"  # write signal (if register is R, )
    WS = "ws"  # set signal
    SW = "sw"  # set signal
    WC = "wc"  # clear signal
    CW = "cw"  # clear signal
    WSC = "wsc"  # clear and set signal (set takes priority over clear)
    WCS = "wcs"  # clear and set signal (clear takes priority over set)
    SWC = "swc"  # clear and set signal (set takes priority over clear)
    CWS = "cws"  # clear and set signal (clear takes priority over set)
    SCW = "scw"  # clear and set signal (set takes priority over clear)
    CSW = "csw"  # clear and set signal (clear takes priority over set)
    WU = "wu"  # logic write port (write strobe + level)
    UW = "uw"  # logic write port (write strobe + level)

    RW = "rw"  # readwrite signal (only if no external write access)
    RWS = "rws"  # read and set signal
    RSW = "rsw"  # read and set signal
    RWC = "rwc"  # read and clear signal
    RCW = "rcw"  # read and clear signal
    RWSC = "rwsc"  # read and clear and set signal (set takes priority over clear)
    RWCS = "rwcs"  # read and clear and set signal (clear takes priority over set)
    RSWC = "rswc"  # read and clear and set signal (set takes priority over clear)
    RCWS = "rcws"  # read and clear and set signal (clear takes priority over set)
    RSCW = "rscw"  # read and clear and set signal (set takes priority over clear)
    RCSW = "rcsw"  # read and clear and set signal (clear takes priority over set)
    RWU = "rwu"  # read and logic write port (write strobe + level)
    RUW = "ruw"  # read and logic write port (write strobe + level)
    
    RCU = "rcu"  # read and clear signal
    RSU = "rsu"  # read and clear signal
    RSCU = "rscu"  # read and clear signal
    
    # RWS = "rs"  # read and set signal
    # RWC = "rc"  # read and clear signal
    # RWSC = "rsc"  # read and clear and set signal
    # RWU = "ru"  # read and logic write port (write strobe + level)

    def read_signal(self):
        return "r" in self.value

    def write_signal(self):
        return "w" in self.value

    def set_signal(self):
        return "s" in self.value

    def clear_signal(self):
        return "c" in self.value

    def update_signal(self):
        return "u" in self.value

    def write_access(self):
        return LogicAccess(re.sub(r"r", "", self.value))

    def add(self, access: "LogicAccess"):
        match access.value:
            case 'w':
                return LogicAccess(re.sub(r"(^r?)w?", r"\1w", self.value))
            case _:
                Log.fatal(f'Could not add access...')


class ReadStrobe(str, Enum):
    NONE = "none"  # do not generate a read strobe
    COMBO = "combo"  # generate a combinatorial read strobe,
    SYNC = "sync"  # generate a flopped read strobe, this will be one cycle delayed
    SYNC_CLEAR = "sync_clear"  # generate a flopped read strobe, to be cleared synchronously
    ASYNC_CLEAR = "async_clear"  # generate a flopped read strobe, to be cleared asynchronously

    def is_combo(self):
        return self in [WriteStrobe.NONE,WriteStrobe.COMBO]

    def needs_reg(self):
        return not self.is_combo()

class WriteStrobe(str, Enum):
    NONE = "none"  # do not generate a write strobe
    COMBO = "combo"  # generate a combinatorial write strobe,
    SYNC = "sync"  # generate a flopped write strobe, this will be one cycle delayed
    SYNC_CLEAR = "sync_clear"  # generate a flopped read strobe, to be cleared synchronously
    ASYNC_CLEAR = "async_clear"  # generate a flopped read strobe, to be cleared asynchronously

    ZERO_COMBO = "zero_combo"  # generate a combinatorial write strobe if write data is 0
    ZERO_SYNC = "zero_sync"  # generate a flopped write strobe, this will be one cycle delayed if write data is 0
    ZERO_SYNC_CLEAR = "zero_sync_clear"  # generate a flopped read strobe, to be cleared synchronously if write data is 0
    ZERO_ASYNC_CLEAR = "zero_async_clear"  # generate a flopped read strobe, to be cleared asynchronously if write data is 0

    ONE_COMBO = "zero_combo"  # generate a combinatorial write strobe if write data is 1
    ONE_SYNC = "zero_sync"  # generate a flopped write strobe, this will be one cycle delayed if write data is 1
    ONE_SYNC_CLEAR = "zero_sync_clear"  # generate a flopped read strobe, to be cleared synchronously if write data is 1
    ONE_ASYNC_CLEAR = "zero_async_clear"  # generate a flopped read strobe, to be cleared asynchronously if write data is 1

    def is_combo(self):
        return self in [WriteStrobe.NONE,WriteStrobe.COMBO,WriteStrobe.ZERO_COMBO,WriteStrobe.ONE_COMBO]

    def needs_reg(self):
        return not self.is_combo()


class Encoding(str, Enum):
    # NUMBER = "number"
    ASCII = "ascii"  # ascii string, 8bit per char
    CHOICE = "choice"  # custom choices, will be child elements
    SIGNED = "signed"  # Two's Complement
    UNSIGNED = "unsigned"  # Straight Binary
    FLOAT = "float"  # floating point number, signed
    UFLOAT = "ufloat"  # floating point number, unsigned
    FIXED = "fixed"  # fixed point number, signed
    UFIXED = "ufixed"  # fixed point number, unsigned

    def signed(self):
        return self.value in ["signed", "float", "fixed"]

    def number(self):
        return self.value not in ["ascii", "choice"]


class BaseNode:
    tags: set[str] = None
    visibility: Visibility = None

    def __init__(self, parent: "BaseNode", name: str, description: str = "", id: str = None, offset: int = None, visibility: Visibility = Visibility.PUBLIC, template: bool = False):
        # Directly managed
        self._id: str | None = id
        self.parent = parent
        self.instance: str | int | None = None
        self.instances = list()
        self.related = list()

        self.name: str = name if name else '{}'
        self.description: str = description
        self.offset: int = offset
        self.base: "BaseNode" | None = None

        self.template: bool = template

        if BaseNode.visibility is None:
            BaseNode.visibility = BaseNode._get_base_attr_prop("_visibility", set())
        self.visibility: Visibility = visibility

        self.children: list["BaseNode"] = list()
        self.templates: list["BaseNode"] = list()

        self.attributes = dict()

        if BaseNode.tags is None:
            BaseNode.tags = BaseNode._get_base_attr_prop("_tags", set())
        self.tags: set[str]= set()

        if parent is not None:
            parent.add_child(self)

    def __del__(self):
        # Log.debug("destructor called")
        if self.parent is not None:
            self.parent.remove_child(self)

    # @property
    # def instance(self):
    #     # value = 0
    #     # for child in self.get_children():
    #     #     value |= (child.get_value()&((1<<32)-1)) << child.offset

    #     return self._instance

    # @instance.setter
    # def instance(self, value):
    #     self._instance = value


    @property
    def id(self) -> str | None:
        if self._id is None:
            self._id = self.get_hier_name()
        return self._id
    
    @id.setter
    def id(self, val: str | None):
        if (self._id is not None) and (val != self._id):
            Log.fatal(f"Overwriting id of {self.name} from {self._id} to {val}")
        self._id = val

    def to_dict(self):
        return {
            "type": self.get_type_name(),
            "name": self.get_name(),
            "description": self.get_description(),
            "offset": self.offset,
            "instance": self.instance,
            "base": self.get_base().id if self.is_instance() is not None else None,
            "children": [c.to_dict() for c in self.get_children()],
            "related": [f.id for f in self.related],
            "id": self.id,
        }

    def _get_base_attr(self, name, default):
        if not hasattr(self.get_base(), name):
            setattr(self.get_base(), name, default)
        return getattr(self.get_base(), name)
        # else:
        #     Log.debug(f'Reading uninitialized class attribute "{name}" ({self.id})')
        #     return None

    def _set_base_attr(self, value, name):
        setattr(self.get_base(), name, value)

    def _get_base_attr_prop(name, value=None):
        return property(partial(BaseNode._get_base_attr, name=name, default=value), partial(BaseNode._set_base_attr, name=name))

    # getters
    def get_description_html(self):
        return html.escape(self.get_description().replace("\n", "<br>"))

    def get_base(self, depth=None):
        if (self.base is None) or (depth == 0):
            return self
        else:
            return self.base.get_base(depth=None if depth is None else depth - 1)

    def is_instance(self, depth=0):
        if (self.base is not None):
            return True
        elif (self.parent is None) or (depth == 0):
            return False
        else:
            return self.parent.is_instance(depth=depth-1)

    def set_offset_if_none(self, offset):
        self.offset = offset if self.offset is None else self.offset

    # def get_total_offset(self):
    #     pass

    def get_raw_name(self):
        return self.get_base().name

    def get_raw_description(self):
        return self.get_base().description

    def get_name(self, raw=False, trim=False) -> str:
        try:
            # return self.get_raw_name().replace("{}", f"{self.instance}")
            def repl(match):
                inst = self.instance if self.instance else self.id
                if (inst is None) and (("$" in match.group(1)) or (match.group(1) == "")):
                    Log.fatal(f"Instance value is None when trying to evaluate formula in name: {self.get_raw_name()} (id: {self._id})")

                formula = match.group(1).replace("$", inst)
                return f"{inst}" if match.group(1) == "" else f"{eval(formula)}"

            if raw:
                return re.sub(r"(^_|_$)","", re.sub(r"__", "_", re.sub(r"{([^}]*)}", "", self.get_raw_name())))
            else:
                return re.sub(r"{([^}]*)}", repl, self.get_raw_name())
        except Exception as e:
            if self._id is not None:
                Log.error(f"Resorting to id instead of name! (name: {self.get_raw_name()}, id: {self._id})")
                return self._id
            Log.fatal(f"Unable to build instance name of (name: {self}, instance: {self.instance}) \n {e}")
            return None

    def get_description(self) -> str:
        try:
            # return self.get_raw_description().replace("{}", f"{self.instance}")
            def repl(match):
                if self.instance is None:
                    inst = self.get_name()
                else:
                    inst = self.instance

                formula = match.group(1).replace("$", inst)
                return f"{inst}" if match.group(1) == "" else f"{eval(formula)}"

            return re.sub(r"{([^}]*)}", repl, self.get_raw_description())
        except Exception as e:
            Log.fatal(f"Unable to build instance description of (name: {self}, instance: {self.instance}) -> {e}")
            # return None

    def get_name_without_instance(self):
        try:
            name = re.sub(r"_?{[^}]*}", "", self.get_raw_name())
            if name == "":
                return self.id
            else:
                return name
        except:
            Log.fatal(f"Unable to build instance name of (name: {self}, instance: {self.instance})")
            return None

    def get_previous_child_node(self):
        if self.parent is not None:
            index = self.parent.get_children().index(self)
            if index > 0:
                return self.parent.get_children()[index - 1]
        return None

    def get_next_child_node(self):
        if self.parent is not None:
            index = self.parent.get_children().index(self)
            if index < (len(self.parent.get_children()) - 1):
                return self.parent.get_children()[index + 1]
        return None

    def get_possible_array_member(self):

        base = self if self.get_base() is None else self.get_base()
        member = [self]
        # if re.search(r'0$', base.get_name()) is None:
        #     return member
        child = self.get_next_child_node()
        while child is not None:
            if child.get_base() == base:
                member.append(child)
                child = child.get_next_child_node()
            else:
                return member
        return member

    def get_possible_array_size(self):
        return len(self.get_possible_array_member())

    def get_instance_index_in_parent(self):
        base = self if self.get_base() is None else self.get_base()
        index = 0
        child = self.get_previous_child_node()
        while child is not None:
            if child.get_base() == base:
                index += 1
                child = child.get_previous_child_node()
            else:
                return index
        return index

    def get_previous_child_node_offset(self, depth=1):
        node = self.get_previous_child_node()
        return 0 if node is None else node.get_offset(depth)

    def get_previous_node(self):
        if self.parent is not None:
            index = self.parent.get_children().index(self)
            if index > 0:
                return self.parent.get_children()[index - 1]
            else:
                return self.parent
        return None

    def get_depth(self):
        node = self
        depth = 0
        while node.parent is not None:
            node = node.parent
            depth +=1
        return depth

    def get_hier_name(self, depth=-1, join_symbol="/", mindepth=0, filter_duplicates=False, use_raw_name=False):
        # if (depth == 1) or (self.parent is None) or (self.get_depth()<=mindepth):
        #     return self.get_name()
        # else:
        #     return f"{self.parent.get_hier_name(depth=depth-1, join_symbol=join_symbol, mindepth=mindepth)}{join_symbol}{self.get_name()}"
        

        node = self
        d = 0
        name = [node.get_name(raw=use_raw_name).strip(join_symbol)]
        while (node.parent is not None) and (depth!=d) and (node.get_depth()>mindepth):
            node = node.parent
            d+=1
            if (node.get_name(raw=use_raw_name) != name[-1]) or not filter_duplicates:
                name.append(node.get_name(raw=use_raw_name).strip(join_symbol))
        return join_symbol.join(reversed(name)).strip(join_symbol)


    def get_markdown(self, tags=None, field_table=False, header=None, filter_instances=False):
        tags = tags if tags is not None else set()
        m = '\n\n'
        fields = [fi for fi in (self.get_children(-1, Field) + ([self] if isinstance(self, Field) else [])) if tags <= fi.get_tags()]
        if field_table:
            header = ["Field", "Description"] if header is None else header
            m += "\\scriptsize\n\n"
            m += tabulate.tabulate([[f.get_name(), f.get_description()] for f in fields], headers=header, tablefmt="grid", maxcolwidths=[None, 64])
            m += "\n: {.striped .small .hover}\n\n\\normalsize\n"
        else:
            header = ["Option", "Description"] if header is None else header
            last_field : Field = None
            for f in fields:
                if filter_instances and last_field is not None and last_field.get_base() == f.get_base():
                    continue
                last_field = f
                m += "\\small\n"
                m += f"**{f.get_hier_name(join_symbol="_", mindepth=1, filter_duplicates=True)}** ({f.width}bit{', signed' if f.encoding.signed() else ''})  \n"
                m += f"{f.get_description()}\n\n"
                if f.has_children():
                    m += "\\scriptsize\n"
                    m += tabulate.tabulate([[c.get_name(), c.get_description()] for c in [c for c in f.get_children()]], 
                                           headers=header, tablefmt="grid", maxcolwidths=[None, 64])
                    m += "\n: {.striped .small .hover}\n\n\\normalsize\n"
                m += f"\n\n"
        return IPython.display.Markdown(m)


    def get_hier_name_without_instance(self):
        if self.parent is None:
            return self.get_name_without_instance()
        else:
            return f"{self.parent.get_hier_name_without_instance()}/{self.get_name_without_instance()}"

    def get_offset(self, depth=1):
        if (depth == 1) or (self.parent is None):
            return self.offset
        else:
            return self.offset + self.parent.get_offset(depth=depth - 1)

    def has_children(self):
        return bool(self.children)

    def get_children(self, depth=1, child_type=None) -> list["Block"]:
        children = list(self.children)
        if depth != 1:
            for child in children:
                children.extend(child.get_children(depth - 1, child_type))
        children = list(child for child in children if ((child_type is None) or (isinstance(child, child_type))))
        return children

    def get_parent(self, parent_type=None):
        if parent_type is None or isinstance(self.parent, parent_type):
            return self.parent
        else:
            return self.parent.get_parent(parent_type=parent_type)

    def get_self_or_parent(self, parent_type=None):
        if parent_type is None or isinstance(self, parent_type):
            return self
        else:
            return self.parent.get_self_or_parent(parent_type=parent_type)

    def get_child(self, id):
        for child in self.get_children(-1):
            if child.id == id:
                return child
        return None

    def get_project(self) -> "Project":
        return self if isinstance(self, Project) else self.parent.get_project()

    def get_node(self, path, type=None):
        # path = re.sub(r'^\${?([^}]*)}?$', '\1', path)

        splits = path.split("/", 1) + ["", ""]

        if path == "":
            return self
        elif splits[0] == "":
            return self.get_project().get_node(splits[1])
        elif splits[0] == "..":
            return self.parent.get_node(splits[1])
        else:
            for child in self.get_children():
                if child.get_name() == splits[0]:
                    return child.get_node(splits[1])

        Log.fatal(f"Unable to lookup {path} from {self.name}")

    def get_child_by_offset(self, offset):
        for child in self.get_children():
            if child.offset == offset:
                return child
        return None

    def get_child_by_name(self, name):
        for child in self.get_children():
            if child.get_name() == name:
                return child
        return None

    # def get_child_by_offset(self, offset, child_type = None):
    #     if self.offset == offset and self.get_type() == child_type:
    #         return self

    #     for child in self.get_children():

    #     self.offset
    #     for child in self.get_children(-1):
    #         if (child.get_uuid() == uuid):
    #             return child
    #     return None

    def get_type(self):
        return type(self)

    def get_type_name(self):
        return self.get_type().__name__

    def get_attributes(self):
        return self.attributes

    def get_attribute(self, name, default, depth=1):
        depth = 0 if depth is None else depth

        if depth == 1 or self.parent is None:
            return self.attributes.get(name, default)
        else:
            return self.attributes.get(name, self.parent.get_attribute(name, default, depth - 1))


    def get_member_values(self, member, vals = None, filter = set([None])) -> set:
        if vals is None:
            vals = set()
        if hasattr(self, member):
            vals |= set([getattr(self, member)])

        for child in self.get_children():
            vals |= child.get_member_values(member, vals)

        return vals - filter

    def get_tags(self, tags=None) -> set:
        if tags is None:
            tags = set()
        tags |= self.tags | {self.get_name()}
        if self.parent is None:
            return tags
        else:
            return self.parent.get_tags(tags)
        

    # def get_offset(self):
    #     return self.offset
    #
    # def get_address(self):
    #     return self.parent.get_address() + self.offset

    # setters
    def set_name(self, name):
        self.get_base().name = name

    def set_description(self, description):
        self.get_base().description = description

    def set_base(self, base):
        self.base = base.get_base()
        self.base.add_instance(self)

        self.children = base.copy_children(self)

    def copy_children(self, parent):
        copies = list()
        for child in self.children:
            Log.debug(f"copy child {child} {child.get_hier_name(use_raw_name=True)}")
            childcopy = copy.copy(child)
            childcopy.parent = parent
            # childcopy.id=(child.get_hier_name())
            if childcopy.get_base() is not None:
                Log.debug("base already present!")
            childcopy.set_base(child)
            childcopy._id = None
            childcopy.related = []
            # childcopy.check_valid()
            copies.append(childcopy)
        return copies

    def set_base_by_name(self):
        search_node = self.parent

        while search_node is not None:
            for child in search_node.get_children():
                if (child.name == self.name) and (child.get_type() == self.get_type()) and (child != self):
                    self.set_base(child)
                    return True
            search_node = search_node.parent
        return False

    def set_base_by_id(self, id):
        search_node = self.parent

        while search_node is not None:
            for child in search_node.get_children(-1):
                if (child != self) and (child.id == id) and (child.get_type() == self.get_type()):
                    self.set_base(child)
                    return True
            search_node = search_node.parent
        return False

    def get_hierachy_depth(self):
        parent = self.parent
        depth = 0
        while parent is not None:
            depth = depth + 1
            parent = parent.parent
        return depth

    def set_attributes(self, attributes, defaults={}):
        if not isinstance(attributes, dict):
            Log.fatal(f"{self} - Attributes must be a dict")
        for key, value in attributes.items():
            if re.match(r"[a-zA-Z]\w*", key) and defaults.get(key) != value:
                self.attributes[key] = value
            else:
                Log.error(f"{self} - attribute key is not valid: {key}")

    def calculate_offsets(self, offset=0):
        for child in self.get_children():
            child.set_offset_if_none(offset)
            child.calculate_offsets()
            if isinstance(child, Block):
                offset = child.offset + child.get_last_register_offset() + 1
            elif isinstance(child, Field):
                offset = child.offset + child.width
            else:
                offset = child.offset + 1

    def add_children_as_attributes(self):
        for child in self.get_children():
            setattr(self, child.get_name(), child)

            if child.get_instance_index_in_parent() == 0 and len(child.get_possible_array_member()) > 1:
                setattr(self, child.get_base().get_name_without_instance(), [child])
            elif child.get_instance_index_in_parent():
                getattr(self, child.get_base().get_name_without_instance()).append(child)

            child.add_children_as_attributes()

    def simplify_formulas(self):
        for field in self.get_children(depth=-1, child_type=Field):
            field : Field
            Log.debug(f"Solving formula {field.formula} for {field.id}")

            formula = f"{field.formula}"
            formula = formula.replace("max(", "Max(")
            formula = formula.replace("min(", "Min(")

            letter_real = "α"
            letter_raw = chr(ord(letter_real) + 1)
            letter = chr(ord(letter_raw) + 1)

            symbol_raw = sympy.Symbol(letter_raw)
            symbol_real = sympy.Symbol(letter_real)

            lookup = {
                letter_real: {"field": field, "string": set("$:REAL"), "type": "REAL", "count": 0, "symbol": symbol_real},
                letter_raw: {"field": field, "string": set("$:RAW"), "type": "RAW", "count": 0, "symbol": symbol_raw},
            }

            for match in re.findall(r"(\$(?:{([^}]*)}|([\w:]*)))", formula):
                Log.debug(f"match: {match}")
                val = (match[1] + match[2]).split(":", 1)
                path = val[0]
                mem = val[1] if len(val) > 1 else "RAW"
                f = field.get_node(path)

                existing = [k for k, v in lookup.items() if (v["field"] == f) and (v["type"] == mem)]

                if existing:
                    formula = formula.replace(match[0], existing[0], 1)
                    lookup[existing[0]]["string"].add(match[0])
                    lookup[existing[0]]["count"] += 1
                else:
                    formula = formula.replace(match[0], letter, 1)
                    lookup[letter] = {"field": f, "string": match[0], "type": mem, "count": 1, "symbol": sympy.Symbol(letter)}
                    field.related.append(f)
                    f.related.append(field)

                    # print(f'lookup {letter} {path} {lookup[letter]["field"].get_name()}')
                    letter = chr(ord(letter) + 1)

            Log.debug(f"formula: {formula}")

            if not lookup[letter_raw]["count"]:
                formula = f"{letter_raw} == {formula}"
            if not lookup[letter_real]["count"]:
                formula = f"{letter_real} == {formula}"

            formula_parts = [p.strip() for p in formula.split("==")]
            Log.debug(f"formula_parts: {formula_parts}")

            formula_parts_extra = [p for p in formula_parts if letter_raw not in p and letter_real not in p]
            formula_parts_raw = [p for p in formula_parts if letter_raw in p]
            formula_parts_real = [p for p in formula_parts if letter_real in p]

            if len(formula_parts_raw) > 1:
                Log.fatal(f"Only one occurance of raw value allowed!")
            if len(formula_parts_real) > 1:
                Log.fatal(f"Only one occurance of real value allowed!")

            if formula_parts_raw[0] == letter_raw:
                exprs_raw = [sympy.parse_expr(p, evaluate=False) for p in formula_parts if p != letter_raw]
            else:
                exprs_raw = [sympy.parse_expr(f"{formula_parts_raw[0]} == {p}", evaluate=False) for p in formula_parts if p != letter_raw]
                exprs_raw = [s for ex in exprs_raw for s in sympy.solve(ex, symbol_raw)]

            if formula_parts_real[0] == letter_real:
                exprs_real = [sympy.parse_expr(p, evaluate=False) for p in formula_parts if p != letter_real]
            else:
                exprs_real = [sympy.parse_expr(f"{formula_parts_real[0]} == {p}", evaluate=False) for p in formula_parts if p != letter_real]
                exprs_real = [s for ex in exprs_real for s in sympy.solve(ex, symbol_real)]

            if field.formula != "$":
                # expr_raw = sympy.solve(exprs, [symbol_common, lookup[letter_raw]["symbol"], lookup[letter_real]["symbol"]], dict=True)
                # print(field.formula)
                pass

            def rep(formula):
                formula = f"{formula}"
                for key, val in lookup.items():
                    # Log.debug(f'rep: {key} with ${{{val["field"].id}:{val["type"]}}}')
                    # Log.debug(f'formula: {formula}')
                    formula = formula.replace(key, f'${{{val["field"].id}:{val["type"]}}}')
                    # Log.debug(f'formula: {formula}')
                return formula

            # field_raw = f'${{{field.id}:RAW}}'
            # field_real = f'${{{field.id}:REAL}}'
            # expr_real = sympy.parse_expr(formula, evaluate=False)
            # expr_real = sympy.parse_expr(formula_real, evaluate=False)
            # expr_real_simple = sympy.simplify(expr_real)
            # expr_raw = sympy.solve(sympy.parse_expr(formula, evaluate=False), symbol_raw)[0]

            # print(f'{expr_real}')

            # print(f'{expr_raw}')

            latex = "\\begin{array}{l}"
            latex += f"{rep(sympy.latex(symbol_real))}"
            for e in exprs_real:
                latex += f"={rep(sympy.latex(e))}"

            if symbol_raw not in exprs_real:
                latex += "\\\\"
                latex += f"{rep(sympy.latex(symbol_raw))}"
                for e in exprs_raw:
                    latex += f"={rep(sympy.latex(e))}"
                # latex += f'{field_real} = {rep(sympy.latex(expr_real))} = {rep(sympy.latex(expr_real_simple))}\\\\'
                # latex += f'{field_raw} = {rep(sympy.latex(expr_raw))}'
            latex += "\\end{array}"
            Log.debug(f"latex: {latex}")

            # field.formula = (f'{rep(sympy.parse_expr(formula, evaluate=False))}')
            field.formula_latex = f"{latex}"
            # field.set_formula_real(f'{rep(expr_real_simple)}')
            # field.set_formula_raw(f'{rep(expr_raw)}')

            # simple_formula = sympy.simplify(expr)
            # field.set_formula_simple(f'{rep(simple_formula)}')

            # Log.debug(f'Got: {simple_formula}')

    # functionality
    def add_child(self, child):
        child = [child] if not isinstance(child, list) else child
        self.children.extend(child)

    def remove_child(self, child):
        child = [child] if not isinstance(child, list) else child
        for c in child:
            if c in self.children:
                self.children.remove(c)
            else:
                pass
                # Log.debug(f"remove_child: {self.get_hier_name()} has no child {c.get_hier_name()}")

    def add_instance(self, instance):
        instance = [instance] if not isinstance(instance, list) else instance
        self.instances.extend(instance)

    def filter_templates(self, exclude_visibilities: list = [Visibility.HIDDEN, Visibility.PRIVATE]):
        self.templates = [c for c in self.children if c.template]
        self.children = [c for c in self.children if not c.template]
        for child in self.get_children():
            child.filter_templates(exclude_visibilities)
        for child in self.templates:
            child.calculate_offsets()

    def check_valid(self, depth=1):
        if (self.get_type() in [Project]) and not self.get_children(depth=-1, child_type=Register):
            Log.fatal(f"Project has not a single register!")

        if (not self.get_project().address_width) or (not (0 < self.get_project().address_width <= 32)):
            Log.fatal(f"Address width is invalid! (width: {self.get_project().address_width})")
        
        if self.visibility in [Visibility.HIDDEN]:
            return

        name = self.get_hier_name()
        if self.get_raw_name() is None:
            Log.fatal(f"Name is None! ({name})")
        # elif not re.match(r"^([A-Z]|{[^}]})(?:_?(?:[A-Z0-9]|{[^}]}))*$", self.get_raw_name()):
        elif not re.match(r"^[A-Z](_?[A-Z0-9])*$", self.get_name()):
            Log.fatal(f"Name is invalid ({self.get_raw_name()}, ({name}))", "NAME_CHECK")
        if self.id is None:
            Log.fatal(f"ID is None! ({name})")

        if (self.description in [None, ""]):
            Log.log(logging.ERROR if self.get_type() in [Choice, Field] else logging.INFO, f"Description is missing! ({name})", "DESCRIPTION")
        if self.description == self.get_name():
            Log.warn(f"Useless description, deleting! ({name})", "DESCRIPTION")
            self.description = ""

        if not isinstance(self.offset, int):
            Log.fatal(f"Offset is missing! ({name})")

            

        if depth != 1:
            for child in self.get_children():
                child.check_valid(depth=depth - 1)

        if self.get_type() in [Field] and self.has_children() and self.get_child_by_offset(self.reset_value) is None:
            Log.fatal(f"Default value is not part of choices! ({name})")

        if self.get_type() in [Register] and self.address >= 1<<self.get_project().address_width:
            Log.fatal(f"Register address is exceeding the address space! ({self.address}/{ 1<<self.get_project().address_width} {name})")

        if self.get_type() in [Field] and self.msb >= self.parent.width:
            Log.fatal(f"Field is exceeding the register width! ({name})")

        if self.get_type() in [Field] and self.get_previous_child_node() is not None:
            prev = self.get_previous_child_node()
            if prev.offset + prev.width > self.offset:
                Log.fatal(f"Field is overlapping with previous field! ({name})")
        
        # if self.offset is None:
        #     Log.fatal(f'Offset is None!')

    def has_public_children(self):
        for child in self.get_children():
            if child.visibility == Visibility.PUBLIC:
                return True
        return False

    def __str__(self):
        return f"{self.name}"


class Project(BaseNode):
    def __init__(self, name, description=None, offset=None, address_width=None):
        BaseNode.__init__(self, parent=None, name=name, description=description, offset=offset, visibility=Visibility.PUBLIC)

        self._regmap: dict[int, "Register"] = dict()
        self.address_width : int | None = address_width

    def get_register_by_address(self, address):
        return self._regmap[address]

    def update_register_addresses(self):
        self._regmap = dict()
        for reg in self.get_children(depth=-1, child_type=Register):
            reg.address = reg.get_offset(-1)
            self._regmap[reg.address] = reg

    def __str__(self):
        return f"{self.name}"


class Block(BaseNode):
    def __init__(self, parent, name=None, description=None, offset=None, visibility: Visibility = Visibility.PUBLIC, id: str = None, template: bool = False):
        BaseNode.__init__(self, parent=parent, name=name, description=description, offset=offset, visibility=visibility, id=id, template=template)
        self.parent: Block | Project

    def __str__(self):
        return f"{self.name}"

    def get_last_register_offset(self):
        offset = 0
        last_child = self
        while last_child.get_type_name() in ["Block"]:
            last_child = last_child.children[-1]
            offset += last_child.offset
        return offset

    # def calculate_offsets(self, offset = 0):
    #     for child in self.get_children:
    #         child.set_offset_if_none(offset)
    #         child.calculate_offsets()
    #         offset = child.offset + (child.get_last_register_offset() + 1 if isinstance(child, Block) else 1)


# @add_mutator(True, ["width"])
class Register(BaseNode):
    width: int = BaseNode._get_base_attr_prop("_width")

    def __init__(self, parent, name=None, description=None, offset=None, width=None, visibility: Visibility = Visibility.PUBLIC, id: str = None, template: bool = False):
        BaseNode.__init__(self, parent=parent, name=name, description=description, offset=offset, visibility=visibility, id=id, template=template)
        self.parent: Block

        self._width = width
        self._address = None
        self._value = self.reset_value
        self._updated = Event()
        self._changed = Event()

    def to_dict(self):
        d = BaseNode.to_dict(self)
        d.update({"address": self.get_offset(-1), "width": self.width, "value": self.value})
        return d

    @property
    def address(self) -> int:
        if self._address is None:
            return self.get_offset(-1)
        else:
            return self._address

    @address.setter
    def address(self, value):
        self._address = value

    @property
    def value(self) -> int:
        return self._value

    @value.setter
    def value(self, value):
        changed = self._value != value
        self._value = value

        self._updated.set()
        self._updated.clear()
        if changed:
            self._changed.set()
            self._changed.clear()

    @property
    def updated(self):
        return self._updated

        # for child in self.get_children():
        #     child.set_value((value >> child.offset) & ((1 << child.width)-1))

    @property
    def reset_value(self) -> int:
        value = 0
        for child in self.get_children():
            value |= (child.reset_value & ((1 << 32) - 1)) << child.offset
        return value

    # def calculate_offsets(self, offset = 0):
    #     for child in self.get_children:
    #         child.set_offset_if_none(offset)
    #         child.calculate_offsets()
    #         offset = child.offset + child.width

    def write(self, value):
        Log.fatal("Register write method is not defined!")

    def read(self) -> int:
        Log.fatal("Register read method is not defined!")

    def monitor(self, priority=1, task="default", duration=None, samples=None):
        Log.fatal("Register monitor method is not defined!")

    def stop_monitor(self, task=None):
        Log.fatal("Register stop monitor method is not defined!")

    def __str__(self):
        return f"{self.name}"


###
### FIELDS
###
class Field(BaseNode):
    width: int = BaseNode._get_base_attr_prop("_width")
    access: Access = BaseNode._get_base_attr_prop("_access")
    reset_value: int = BaseNode._get_base_attr_prop("_reset_value")

    # data type
    encoding: Encoding = BaseNode._get_base_attr_prop("_encoding")
    exponent: int | float = BaseNode._get_base_attr_prop("_exponent")
    mantissa: int | float = BaseNode._get_base_attr_prop("_mantissa")
    fraction: int | float = BaseNode._get_base_attr_prop("_mantissa")
    min: int | float = BaseNode._get_base_attr_prop("_min")
    max: int | float = BaseNode._get_base_attr_prop("_max")
    formula: str = BaseNode._get_base_attr_prop("_formula")
    unit: str = BaseNode._get_base_attr_prop("_unit")

    # implementation details
    status: Status = BaseNode._get_base_attr_prop("_status", Status.IMPLEMENTED)
    logic_access: LogicAccess = BaseNode._get_base_attr_prop("_logic_access", LogicAccess.NONE)
    clock_signal: str = BaseNode._get_base_attr_prop("_clock_signal", "clk")
    reset_signal: str = BaseNode._get_base_attr_prop("_reset_signal", "rst_n")
    write_strobe: WriteStrobe = BaseNode._get_base_attr_prop("_write_strobe", WriteStrobe.NONE)
    read_strobe: ReadStrobe = BaseNode._get_base_attr_prop("_read_strobe", ReadStrobe.NONE)
    read_enable: bool | str = BaseNode._get_base_attr_prop("_read_enable", False)
    write_enable: bool | str = BaseNode._get_base_attr_prop("_write_enable", False)

    def __init__(self, parent, name=None, description=None, offset=None, visibility: Visibility = Visibility.PUBLIC, omit=False, id: str = None, template: bool = False):
        BaseNode.__init__(self, parent=parent, name=name, description=description, offset=offset, visibility=visibility, id=id, template=template)
        self.parent: Register
        
        self.omit = omit
        self.formula_real = None
        self.formula_raw = None
        self.formula_latex = None

        self.changed = Event()

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.offset

    @property
    def msb(self) -> int:
        return self.offset+self.width-1

    @property
    def lsb(self) -> int:
        return self.offset

    @property
    def value(self) -> int:
        return (self.parent.value & self.mask) >> self.offset

    @value.setter
    def value(self, val: int):
        self.parent.value = (self.parent.value & ~self.mask) | ((val << self.offset) & self.mask)

    @property
    def updated(self) -> Event:
        return self.parent.updated

    # def calculate_offsets(self, offset = 0):
    #     for child in self.get_children:
    #         child.set_offset_if_none(offset)
    #         child.calculate_offsets()
    #         offset = child.offset + 1

    def write(self, value):
        Log.info(f"Writing value {value} to field {self.get_hier_name()}")
        val = self.parent.read()
        val = (val & ~self.mask) | ((value << self.offset) & self.mask)
        self.parent.write(val)

    def read(self):
        Log.info(f"Reading value from field {self.get_hier_name()}")
        self.parent.read()
        return self.value

    def monitor(self, priority=1, task="default", duration=None, samples=None):
        Log.info(f"Monitoring field {self.get_hier_name()}")
        self.get_parent(priority, task, duration, samples)


    def wait(self, above=None, below=None, equal=None, delta=None, abs_delta=None):
        initial_value = self.value

        if equal is not None:
            if above is None and below is None:
                above = equal-1
                below = equal+1
            else:
                Log.fatal("wait equal cannot be combined with above/below")
        if delta is not None:
            if above is None and below is None:
                if delta <= 0:
                    above = initial_value + delta - 1
                else:
                    below = initial_value - delta + 1
            else:
                Log.fatal("wait delta cannot be combined with above/below")
        if abs_delta is not None:
            if above is None and below is None:
                above = initial_value + delta - 1
                below = initial_value - delta + 1
            else:
                Log.fatal("wait abs_delta cannot be combined with above/below")
        
        if above is not None and below is None:
            below = 1<<self.width
        elif below is not None and above is None:
            above = -(1<<self.width)-1

        while True:
            self.parent._changed.wait(timeout=0.1)
            value = self.value

            if above is not None and below is not None:
                if (value > above) and (value < below):
                    return
                elif (above > below) and ((value > above) or (value < below)):
                    return



    def set_default_min_max(self):
        self.min = [0, -(1 << (self.width - 1))][self.encoding.signed()]
        self.max = (1 << (self.width - self.encoding.signed())) - 1

    # def wait_exact(self, value, time, timeout = None):
    #     self.wait_within(value, value, time, timeout)

    # def wait_above(self, value, time, timeout = None):
    #     self.wait_within(value, maxval, time, timeout)

    # def wait_below(self, value, time, timeout = None):
    #     self.wait_within(minval, value, time, timeout)

    # def wait_delta(self, value, time, timeout = None):
    #     threshold = self.read() + value
    #     if value >= 0:
    #         self.wait_above(threshold, time, timeout)
    #     else:
    #         self.wait_below(threshold, time, timeout)

    # def wait_within(self, min_value, max_value, time, timeout):
    #     pass

    # def wait_outside(self, min_value, max_value, time, timeout):
    #     pass

    def to_dict(self):
        d = BaseNode.to_dict(self)
        d.update(
            {
                "access": self.access,
                "width": self.width,
                "reset": self.reset_value,
                "unit": self.unit,
                "value": self.value,
                # "value_timestamp": self.get_value_timestamp(),
                # "encode": self.encode,
                "min": self.min,
                "max": self.max,
                # "float_mantissa": self.float_mantissa,
                # "float_exponent": self.float_exponent,
                # "fixed_fraction": self.fixed_fraction,
                # "formula": self.formula,
                # "formula_real": self.get_formula_real(),
                # "formula_raw": self.get_formula_raw(),
                "formula_latex": self.formula_latex,
            }
        )
        return d

    def __str__(self):
        return f"{self.name}"


###
### FIELD OPTIONS
###
class Choice(BaseNode):
    def __init__(self, parent, name=None, description=None, offset=None, visibility: Visibility = Visibility.PUBLIC, id: str = None, template: bool = False):
        BaseNode.__init__(self, parent, name, description=description, offset=offset, visibility=visibility, id=id, template=template)
        self.parent: Field
    
    def __str__(self):
        return f"{self.name}"
    
    def select(self):
        Log.info(f"Selecting choice {self.name} for {self.parent.get_hier_name()}")
        self.parent.write(self.offset)



class Log:
    ignores: list = []
    demotes: list = []

    @staticmethod
    def setup(log_level, args=None):
        for name,val in args.__dict__.items():
            if name.startswith("ignore_") and val:
                Log.ignores.append(name.replace("ignore_", "").upper())
            if name.startswith("demote_") and val:
                Log.demotes.append(name.replace("demote_", "").upper())
        logging.basicConfig(stream=sys.stdout, level=log_level, format="%(levelname)8s: %(message)s")

    @staticmethod
    def log(severity, msg, type = None):
        if type in Log.ignores:
            return
        elif type in Log.demotes:
            severity = {logging.FATAL: logging.ERROR, logging.ERROR: logging.WARNING, logging.WARNING: logging.INFO, logging.INFO: logging.DEBUG}.get(severity, severity)
         
        logging.log(severity, msg)
        if severity >= logging.FATAL:
            if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
                raise RuntimeError(msg)
            else:
                logging.log(severity, "Exiting, try running with more debug informations (-vvv) to get a better understanding of the issue")
                sys.exit(1)

    @staticmethod
    def fatal(msg):
        Log.log(logging.FATAL, msg, type = None)

    @staticmethod
    def error(msg):
        Log.log(logging.ERROR, msg, type = None)

    @staticmethod
    def warn(msg):
        Log.log(logging.WARN, msg, type = None)

    @staticmethod
    def info(msg):
        Log.log(logging.INFO, msg, type = None)

    @staticmethod
    def debug(msg):
        Log.log(logging.DEBUG, msg, type = None)


class Parser:
    def __init__(self):
        self.args = None

    def get_argparse(self):
        Log.fatal(f"Selected Parser is missing get_argparse")

    def set_args(self, args):
        Log.fatal(f"Selected Parser is missing set_args")

    def parse(self):
        Log.fatal(f"Selected Parser is missing parse")


class Composer:
    def __init__(self):
        self.args = None

    def get_argparse(self):
        Log.fatal(f"Selected Composer is missing get_argparse")

    def set_args(self, args):
        Log.fatal(f"Selected Composer is missing set_args")

    def compose(self, project):
        Log.fatal(f"Selected Composer is missing compose")



def regscribe(cmdline: list[str], parser="xml", composer="project", dump=None):
    return main(cmdline, default_parser=parser, default_composer=composer, default_dump=dump)

def main(cmdline=None, default_composer="xml", default_parser="xml", default_dump=None):
    external_path = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), f'../../'))
    parsers = glob(os.path.join(os.path.dirname(__file__), "parse", "*.py")) + glob(os.path.join(external_path, "parse", "*.py"))
    parsers = [re.sub(r"(\w+).py", r"\g<1>", os.path.basename(x)) for x in parsers]
    composers = glob(os.path.join(os.path.dirname(__file__), "compose", "*.py")) + glob(os.path.join(external_path, "compose", "*.py"))
    composers = [re.sub(r"(\w+).py", r"\g<1>", os.path.basename(x)) for x in composers]

    argparser = argparse.ArgumentParser(add_help=False)
    group = argparser.add_argument_group("Generic Arguments")
    group.add_argument("-p", "--parser", choices=parsers, default=default_parser, help="Selects the used parser")
    group.add_argument("-c", "--composer", choices=composers+["project"], default=default_composer, help="Selects the used composer")
    group.add_argument("-v", "--verbose", action="count", default=0, help="Verbosity level")
    group.add_argument("--demote_name_check", action="store_true", help="Accept invalid names")
    group.add_argument("--skip_validity_check", action="store_true", help="Skip validity checks, may lead to broken outputs or crashes")
    group.add_argument("--ignore_description", action="store_true", help="Ignore missing descriptions")
    group.add_argument("--dump", type=Path, default=default_dump, help="Dump the parsed project to a pickle file for later reuse, creates file if not existing or outdated")

    args, remaining = argparser.parse_known_args(cmdline)
    arg_composer:str = args.composer
    arg_parser:str = args.parser
    arg_verbose:int = args.verbose
    arg_skip_validity_check:bool = args.skip_validity_check
    arg_dump:str = args.dump

    Log.setup([logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG][min(arg_verbose, 3)], args)

    # get parser arguments

    if importlib.util.find_spec(f"regscribe.parse.{arg_parser}") is not None:
        parser_module = importlib.import_module(f"regscribe.parse.{arg_parser}")
    else:
        spec = importlib.util.spec_from_file_location(f"parse_{arg_parser}", os.path.abspath(os.path.join(external_path, 'parse', f'{arg_parser}.py')))
        parser_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser_module)

    parser: Parser = parser_module.get_parser()
    parse_parents = [parser.get_argparse()]

    # get composer arguments
    if arg_composer != "project":
        if importlib.util.find_spec(f"regscribe.compose.{arg_composer}") is not None:
            composer_module = importlib.import_module(f"regscribe.compose.{arg_composer}")
        else:
            spec = importlib.util.spec_from_file_location(f"compose_{arg_composer}", os.path.abspath(os.path.join(external_path, 'compose', f'{arg_composer}.py')))
            composer_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(composer_module)


        composer: Composer = composer_module.get_composer()
        parse_parents.append(composer.get_argparse())

    argparser = argparse.ArgumentParser(
        # formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        prog="regscribe",
        description="Parsing script to convert from one format to another",
        epilog="Specify parser / composer to see their options",
        parents=[argparser] + parse_parents,
    )

    # if not cmdline:
    #     argparser.print_help(sys.stderr)
    #     sys.exit(1)

    args = argparser.parse_args(cmdline)

    # eval args
    Log.info(f"Input Arguments: {args}")

    # eval common arg_..
    try:
        if args.input.startswith("http"):
            Log.info(f"Downloading: {args.input}")
            r = requests.get(args.input, allow_redirects=True)
            if r.ok:
                open(".converter_input", "wb").write(r.content)
                args.input = ".converter_input"
            else:
                Log.fatal(f"Download failed: status code {r.status_code}\n{r.text}")
    except Exception:
        pass

    # Parse
    parser.set_args(args)

    project: Project | None = None

    pickle_files = glob(os.path.join(external_path, "parse" ,"*.py"))
    pickle_files += glob(os.path.join(os.path.dirname(__file__), "parse" ,"*.py"))
    pickle_files += glob(os.path.join(external_path, "compose" ,"*.py"))
    pickle_files += glob(os.path.join(os.path.dirname(__file__), "compose" ,"*.py"))
    pickle_files += glob(os.path.join(os.path.dirname(__file__), "converter.py"))
    pickle_args = f"{args.__dict__}"
    pickle_timestamp = f"{sum([os.path.getmtime(os.path.abspath(os.path.realpath(f))) for f in pickle_files])}"

    if arg_dump and os.path.exists(arg_dump):

        with open(arg_dump, 'rb') as projfile:
            project: Project = pickle.load(projfile)
            if (project.pickle_args == pickle_args) and (project.pickle_timestamp == pickle_timestamp):
                Log.info(f"Using cached project from pickle dump {arg_dump}")
            else:
                Log.info(f"Pickle dump {arg_dump} is outdated, reparsing")
                project = None
    
    if not project:
        project: Project = parser.parse()
        project.add_children_as_attributes()
        project.update_register_addresses()
        project.pickle_args = pickle_args
        project.pickle_timestamp = pickle_timestamp
        
        if not arg_skip_validity_check:
            project.check_valid(depth=-1)
        if arg_dump is not None:
            with open(arg_dump, 'wb') as projfile:
                pickle.dump(project, projfile, pickle.HIGHEST_PROTOCOL)
                Log.info(f"Dumped project to pickle file {arg_dump}")
    

    if arg_composer == "project":
        return project

    # Composer
    composer.set_args(args)
    composer.compose(project)

    # with open("dbgdict.json", 'w') as f:
    #     f.write(f'{json.dumps(project.to_dict(), indent=4)}')
    exit(0)


if __name__ == "__main__":
    print("Do not execute converter directly, call regscribe instead! ('python -m regscribe' or just 'regscribe')")
    exit(1)
