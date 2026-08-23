import argparse
import logging
import os
import pathlib
import re
from datetime import datetime
from pathlib import Path
from enum import Enum, auto

from matplotlib import container
from regscribe.converter import *

# Create local logger
logger = logging.getLogger(__name__)

def get_composer():
    return compose_sv()


class compose_sv(Composer):
    def __init__(self):
        pass

    def get_argparse(self):
        argparser = argparse.ArgumentParser(add_help=False)
        group = argparser.add_argument_group("Systemverilog Interface Builder Arguments")
        group.add_argument("-o", "--output", type=Path, default="out.xml", help="Output base filename, will generate a .sv a _pkg.sv and a .svh file")
        group.add_argument("--hier_start", type=int, default=1, help="Name hierachy depth")
        return argparser

    def set_args(self, args):
        self.output: Path = args.output
        self.hier_start = args.hier_start

    def compose(self, project: Project):
        logger.info(f"Creating SV file")

        self.project = project

        self.registers: list[Register] = self.project.get_children(-1, Register)
        self.fields: list[Field] = self.project.get_children(-1, Field)
        self.fields_noinstance: list[Field] = [f for f in self.fields if not f.is_instance()]
        self.regwidth = self.registers[0].width

        self.reset_signals = self.project.get_member_values("reset_signal")
        self.clock_signals = self.project.get_member_values("clock_signal")
        self.read_access_signals = self.project.get_member_values("read_access")
        self.write_access_signals = self.project.get_member_values("write_access")
        self.read_enable_signals = self.project.get_member_values("read_enable")
        self.write_enable_signals = self.project.get_member_values("write_enable")
        
        self.write_data_signal = "write_data"
        self.read_data_signal = "read_data"
        self.write_addr_signal = "write_addr"
        self.read_addr_signal = "read_addr"
        self.read_access_signal = "read_access"
        self.write_access_signal = "write_access"
                
        self.signals: svSignal = []

        self.build_constants()
        self.build_packages()                    
        self.generateModule()                    
        self.generateInterface()                    
    
    def build_constants(self):
        out = svOutputHelper()
        out.add(f'`ifndef {self.output.name.upper()}__SVH')
        out.add(f'`define {self.output.name.upper()}__SVH')

        out.add(f'`define {self.output.name.upper()}_ADDRESS_WIDTH {self.project.address_width}')
        out.add(f'`define {self.output.name.upper()}_DATA_WIDTH {self.regwidth}')

        out.add(f'`ifdef MAX_COMPATIBILITY')
        out.add(f'{"`define REGSCRIBE_ENUM_TYPE(pkg, kind, msb) ":64}kind logic [msb:0]')
        out.add(f'{"`define REGSCRIBE_ENUM_CAST(pkg, value) ":64}value')
        out.add(f'{"`define REGSCRIBE_ENUM_RESET(pkg, name, value) ":64}value')
        out.add(f'`else')
        out.add(f'{"`define REGSCRIBE_ENUM_TYPE(pkg, kind, msb) ":64}kind pkg::t')
        out.add(f'{"`define REGSCRIBE_ENUM_CAST(pkg, value) ":64}pkg::t\'(value)')
        out.add(f'{"`define REGSCRIBE_ENUM_RESET(pkg, name, value) ":64}pkg::name')
        out.add(f'`endif')

        out.add(f'// register defines')        
        out.add(f'`define BASEMODPORT \\', '>+1', container='port_monitor')        

        for register in self.registers:            
            register_hiername = register.get_hier_name(join_symbol="_", mindepth=self.hier_start, filter_duplicates=True)
            out.add(f'{f"`define {register_hiername}_ADDR":64}(`{self.output.name.upper()}_ADDRESS_WIDTH\'(\'d{register.address}))')
                      
            for field in register.get_children():
                field_hiername = field.get_hier_name(join_symbol="_", mindepth=self.hier_start, filter_duplicates=True)
                field_hiername_raw = field.get_hier_name(join_symbol="_", mindepth=self.hier_start, filter_duplicates=True, use_raw_name=True)
                if(not field.is_instance(depth=-1)):
                    out.add(f'{f"`define {field_hiername_raw}_WIDTH ":64}{field.width}')
                    reset_value = f'(`{field_hiername_raw}_WIDTH\'(\'h{field.reset_value:X}))'
                    if field.has_children():
                        out.add(f'{f"`define {field_hiername_raw}_RESET ":64}`REGSCRIBE_ENUM_RESET({field_hiername_raw}_pkg, {field.get_child_by_offset(field.reset_value).get_name()}, {reset_value})')
                    else:
                        out.add(f'{f"`define {field_hiername_raw}_RESET ":64}{reset_value}')
                out.add(f'{f"`define {field_hiername}_SHIFT ":64}{field.offset}')
                out.add(f'{f"`define {field_hiername}_LSB ":64}`{field_hiername}_SHIFT')
                out.add(f'{f"`define {field_hiername}_MSB ":64}(`{field_hiername}_SHIFT+`{field_hiername_raw}_WIDTH-1)')
                out.add(f'{f"`define {field_hiername}_MASK ":64}((2 << `{field_hiername_raw}_WIDTH) - 1)')
                out.add(f'{f"`define {field_hiername}_REGMASK ":64}(`{field_hiername}_MASK << `{field_hiername}_SHIFT)')
                out.add(f'{f"`define {field_hiername}_TOREG(a) ":64}((``a`` << `{field_hiername}_SHIFT) & `{field_hiername}_REGMASK)')
                         
                out.add(f'`ifdef MPOUT_{field_hiername} `undef MPOUT_{field_hiername} output `else input `endif {field_hiername}, \\', container='port_monitor')

            out.add(f'')

        out.filter_last(',', container='port_monitor')
        out.add(f'', container='port_monitor')
        out.merge('port_monitor')
        out.add(f'\n`endif')
        out.write_to_file(f'{self.output}.svh')
    
    def build_packages(self):
        out = svOutputHelper()
        # out.add_header(self.xmlParser);

        out.add(f'`ifndef {self.output.name.upper()}_PKG__SV')
        out.add(f'`define {self.output.name.upper()}_PKG__SV')

        out.add(f'`include "{self.output.name}.svh"')
        out.add(f'/* verilator lint_save */')
        out.add(f'/* verilator lint_off DECLFILENAME */')
        out.add(f'/* svlint off package_identifier_matches_filename */')
        out.add(f'/* svlint off re_required_package */')

        for field in self.fields_noinstance:
            field_hiername = field.get_hier_name(join_symbol="_", mindepth=self.hier_start, filter_duplicates=True, use_raw_name=True)

            if field.has_children():
                out.add(f'package {field_hiername}_pkg;', '>+1')
                out.add(f'typedef enum logic [`{field_hiername}_WIDTH-1:0]{{','>+1')
                for choice in field.get_children():
                    out.add(f'{choice.get_name():32} = `{field_hiername}_WIDTH\'d{choice.offset},')
                out.filter_last(',')
                out.add(f'}} t;', '<-1')
                out.add(f'endpackage\n', '<-1')

        out.add(f'/* svlint on package_identifier_matches_filename */')
        out.add(f'/* svlint on re_required_package */')
        out.add(f'/* verilator lint_restore */')
        out.add(f'`endif')
        
        out.write_to_file(f'{self.output}_pkg.sv')
  
    def generateModule(self):         
        out = svOutputHelper()
        def to_range(msb, lsb, filled = False) -> str:
            return f"[{lsb}]" if msb == lsb else f"[{msb}:{lsb}]"
        
        out.add(f'`ifndef {self.output.name.upper()}__SV')
        out.add(f'`define {self.output.name.upper()}__SV')

        out.add(f'`include "{self.output.name}.svh"')

        out.add(f'/* verilator lint_save */')
        out.add(f'/* verilator lint_off DECLFILENAME */')
        out.add(f'/* verilator lint_off UNUSED */')
        out.add(f'/* svlint off interface_identifier_matches_filename */')
        out.add(f'/* svlint off re_required_port_output */')
        out.add(f'/* svlint off re_required_port_interface */')

        out.add(f'module {self.output.name}(', '>+1')

        for reset in sorted(list(self.reset_signals)):
            out.add(f'input  logic {reset},')
                        
        for clock in sorted(list(self.clock_signals)):
            out.add(f'input  logic {clock},')
        out.add(f'')
        
        for sig in sorted(list({self.read_addr_signal, self.write_addr_signal})):
            out.add(f'input  logic [`{self.output.name.upper()}_ADDRESS_WIDTH-1:0] {sig},')
            
        for sig in sorted(list([self.read_access_signal] + [self.write_access_signal])):
            out.add(f'input  logic {sig},')
 
        for sig in sorted(list([self.write_data_signal])):
            out.add(f'input  logic [`{self.output.name.upper()}_DATA_WIDTH-1:0] {sig},')
        
        for sig in sorted(list([self.read_data_signal])):
            out.add(f'output logic [`{self.output.name.upper()}_DATA_WIDTH-1:0] {sig},')

        out.add(f'')

        for sig in sorted(list(self.read_access_signals | self.write_access_signals)):
            out.add(f'input  logic {sig},')
      
        for sig in sorted(list(self.read_enable_signals | self.write_enable_signals)):
            out.add(f'input  logic {sig},')
    
        out.add(f'regmap_if.regmap I,')


        out.filter_last(',')
        out.add(f');', '<-1')        
        out.add(f'', '>+1')        
        out.add('')
        
        def declare_signal(node: BaseNode, suffix = None, assign = None, width = None, type='wire', assign_if='', assign_else='\'0', input=False, enum_type=None) -> str:
            width_name = None
            if width is None:
                width = node.width
                if isinstance(node, Field):
                    width_name = f'`{node.get_hier_name(mindepth=self.hier_start, join_symbol="_", filter_duplicates=True, use_raw_name=True)}_WIDTH'
                elif width == self.regwidth:
                    width_name = f'`{self.output.name.upper()}_DATA_WIDTH'
            suffix = '' if suffix is None else f"_{suffix}"
            name = f'{node.get_hier_name(mindepth=self.hier_start, join_symbol="_", filter_duplicates=True)}{suffix}'

            if assign is not None:
                expr = f"({assign_if}) ? ({assign}) : ({assign_else})" if assign_if else f"{assign}"
                if enum_type is not None:
                    expr = f"`REGSCRIBE_ENUM_CAST({enum_type}, {expr})"
                out.add(f"assign I.{name:32} = {expr};")
            self.signals.append(svSignal(name, type, width, input=input, enum_type=enum_type, width_name=width_name))
            return f"I.{name}"

        for reset in self.reset_signals:
            resedge = "posedge" if False else "negedge"
            for clock in self.clock_signals:
                out.add(f'always_ff @(posedge {clock}, {resedge} {reset}) begin : assign_{reset}_{clock}', '<=1', start='\n', container=f'assign_{reset}_{clock}_1')
                out.add(f'if (!{reset}) begin', '<>+1', container=f'assign_{reset}_{clock}_1')
                out.add(f'end else begin', '<=2', container=f'assign_{reset}_{clock}_2')
        for reset in self.reset_signals:
            for clock in self.clock_signals:
                out.add(f'always_ff @(posedge {clock}, negedge {reset}) begin : assign_{reset}_{clock}_strobe', '<=1', start='\n', container=f'assign_{reset}_{clock}_strobe_1')
                out.add(f'if (!{reset}) begin', '<>+1', container=f'assign_{reset}_{clock}_strobe_1')
                out.add(f'end else begin', '<=2', container=f'assign_{reset}_{clock}_strobe_2')
                   



        for register in self.registers:
            register_hiername = register.get_hier_name(join_symbol="_", mindepth=self.hier_start, filter_duplicates=True)

            out.add(f'// Generic - Register: {register_hiername} Width: {register.width} Reset: 0x{register.reset_value:X}')           
            out.add(f'// Description - {register.get_description_html()}')                     
            reg_write_access = declare_signal(register, "reg_write_access", f'{self.write_access_signal} && ({self.write_addr_signal} == `{register_hiername}_ADDR)', 1)
            reg_read_access = declare_signal(register, "reg_read_access", f'{self.read_access_signal} && ({self.read_addr_signal} == `{register_hiername}_ADDR)', 1)

            bitpos = 0
            fieldstring = ''
            fieldstring_read_enable = ''
            for field in sorted(register.get_children(), key=lambda x: x.offset, reverse=False):
                field: Field
                
                field_hiername = field.get_hier_name(join_symbol="_", mindepth=self.hier_start, filter_duplicates=True)
                field_hiername_raw = field.get_hier_name(join_symbol="_", mindepth=self.hier_start, filter_duplicates=True, use_raw_name=True)

                enum_type = f'{field_hiername_raw}_pkg' if field.has_children() else None
                acc = {Access.W: None, Access.W0: 0, Access.W1: 1}[field.access.write_if()]

                field_reset = declare_signal(field, "reset", f'`{field_hiername_raw}_RESET')
                field_write_data = declare_signal(field, "write_data", f'{self.write_data_signal}[`{field_hiername}_MSB:`{field_hiername}_LSB]')
                field_read_access = declare_signal(field, "read_access", f'{reg_read_access}', 1, assign_if=field.read_enable)
                field_write_access = declare_signal(field, "write_access", f"{reg_write_access}{'' if acc is None else f' && ({field_write_data} == \'{acc})'}", 1, assign_if=field.write_enable)
                
  

                out.add(f'// Generic - Register: {register_hiername}{to_range(field.msb, field.lsb)} (0x{register.address:08X})<br>Field: {field.get_name()}<br>Access: {field.access}<br>Logic Access: {field.logic_access}<br>Reset: 0x{field.reset_value:X}',container="fielddesc")           
                out.add(f'// Description - {field.get_description_html()}', container="fielddesc")
                if field.has_children():
                    txt = "// Choices - "
                    for choice in field.get_children():
                        txt += f'0x{choice.offset:02X} {choice.get_name()} {choice.get_description()}<br>'
                    out.add(f'{txt}', container="fielddesc")


                if field.logic_access in [LogicAccess.NONE] and not field.access.can_write():
                    out.merge("fielddesc")
                    field_name = declare_signal(field, assign=f'{field_reset}', type=f'wire{" signed" if field.encoding.signed() else ""}', enum_type=enum_type)
                elif field.logic_access.write_access() in [LogicAccess.W]:
                    out.merge("fielddesc")
                    field_name = declare_signal(field, type=f'logic{" signed" if field.encoding.signed() else ""}', input=True, enum_type=enum_type)
                else:
                    field_name_q = declare_signal(field, suffix="q", type="logic")
                    out.merge("fielddesc")
                    field_name = declare_signal(field, assign=f'{field_name_q}', type=f'wire{" signed" if field.encoding.signed() else ""}', enum_type=enum_type)

                    if field.logic_access.set_signal():
                        field_set = declare_signal(field, "set", type='logic', width=1, input=True)
                    if field.logic_access.clear_signal():
                        field_clear = declare_signal(field, "clear", type='logic', width=1, input=True)
                    if field.logic_access.update_signal():
                        field_value = declare_signal(field, "value", type='logic', input=True)
                        field_update = declare_signal(field, "update", type='logic', width=1, input=True)

                    val = {Access.W: field_write_data, Access.WC: "'0", Access.WS: "'1", Access.WT: f"~{field_write_data}"}[field.access.write_value()]
                    d_assign = f"{field_name_q}"
                    if field.logic_access.write_access() in [LogicAccess.NONE]:
                        d_assign = f"{field_write_access} ? {val} : ({d_assign})"
                    else:
                        for acc in field.logic_access.write_access().value[::-1]:
                            if acc == 'w':
                                d_assign = f"{field_write_access} ? {val} : ({d_assign})"
                            elif acc == 's':
                                d_assign = f"{field_set} ? '1 : ({d_assign})"
                            elif acc == 'c':
                                d_assign = f"{field_clear} ? '0 : ({d_assign})"
                            elif acc == 'u':
                                d_assign = f"{field_update} ? {field_value} : ({d_assign})"
                            else:
                                Log.fatal(f'LogicAccess write type not supported ({field.get_name(), field.logic_access})')
                            
                    field_name_d = declare_signal(field, "d", d_assign)
                    
                    out.add(f"{field_name_q:32} <= {field_reset};", "<=3", container=f'assign_{field.reset_signal}_{field.clock_signal}_1')
                    out.add(f"{field_name_q:32} <= {field_name_d};", "<=3", container=f'assign_{field.reset_signal}_{field.clock_signal}_2')
                    
                if field.write_strobe.needs_reg():
                    field_write_strobe = declare_signal(field, "write_strobe", type='logic')
                    if field.write_strobe in [WriteStrobe.SYNC]:
                        out.add(f"{field_write_strobe:32} <= '0;", "<=3", container=f'assign_{field.reset_signal}_{field.clock_signal}_strobe_1')
                        out.add(f"{field_write_strobe:32} <= {field_write_access};", "<=3", container=f'assign_{field.reset_signal}_{field.clock_signal}_strobe_2')
                    else:
                        Log.fatal(f'Writestrobe {field.write_strobe} is not supported! ({field_hiername})')

                if field.read_strobe.needs_reg():
                    field_read_strobe = declare_signal(field, "read_strobe", type='logic')
                    if field.read_strobe in [ReadStrobe.SYNC]:
                        out.add(f"{field_read_strobe:32} <= '0;", "<=3", container=f'assign_{field.reset_signal}_{field.clock_signal}_strobe_1')
                        out.add(f"{field_read_strobe:32} <= {field_write_access};", "<=3", container=f'assign_{field.reset_signal}_{field.clock_signal}_strobe_2')
                    else:
                        Log.fatal(f'Readstrobe {field.read_strobe} is not supported! ({field_hiername})')



                if field.offset != bitpos:
                    fieldstring = f', {field.offset-bitpos}\'d0' + fieldstring
                    fieldstring_read_enable = f', {field.offset-bitpos}\'d0' + fieldstring_read_enable
                fieldstring = f', {field_name}' + fieldstring
                fieldstring_read_enable = (f', {field.read_enable} ? {field_name} : {field.width}\'d0' if field.read_enable else  f', {field_name}') + fieldstring_read_enable
                bitpos = field.offset+field.width

            if register.width != bitpos:
                fieldstring = f', {register.width-bitpos}\'d0' + fieldstring
                fieldstring_read_enable = f', {register.width-bitpos}\'d0' + fieldstring_read_enable
                    
            out.add(f'// Build Register')           
            declare_signal(register, "reg", assign = "{ " + fieldstring[2:] + " }")
            declare_signal(register, "reg_read_enable", assign = "{ " + fieldstring_read_enable[2:] + " }")
            out.add('')


        for reset in self.reset_signals:
            for clock in self.clock_signals:
                if [field for field in self.project.get_children(-1, Field) if ((field.reset_signal == reset) and (field.clock_signal == clock))]:
                    out.merge(f'assign_{reset}_{clock}_1')
                    out.merge(f'assign_{reset}_{clock}_2')
                    out.add(f'end', '<-1')
                    out.add(f'end', '<-1')

        for reset in self.reset_signals:
            for clock in self.clock_signals:
                if [field for field in self.project.get_children(-1, Field) if ((field.reset_signal == reset) and (field.clock_signal == clock) and (field.write_strobe.needs_reg() or field.read_strobe.needs_reg()))]:
                    out.merge(f'assign_{reset}_{clock}_strobe_1')
                    out.merge(f'assign_{reset}_{clock}_strobe_2')
                    out.add(f'end', '<-1')
                    out.add(f'end', '<-1')

        out.add(f'always_comb begin', '>+1', start='\n');            
        out.add(f'case ({self.read_addr_signal})', '>+1')
        for register in self.registers:
            out.add(f'{self.project.address_width}\'d{register.address:<3}: {self.read_data_signal} = I.{register.get_hier_name(join_symbol="_", mindepth=self.hier_start, filter_duplicates=True)}_reg_read_enable;')
        out.add(f"default: {self.read_data_signal} = '0;")
        out.add(f'endcase', '<-1')
        out.add(f'end', '<-1')
        out.add(f'`include "{self.output.name}_modports.svh"')
        out.add(f'endmodule\n', '<-1')
        out.add(f'/* svlint on interface_identifier_matches_filename */')
        out.add(f'/* svlint on re_required_port_output */')
        out.add(f'/* svlint on re_required_port_interface */')
        out.add(f'/* verilator lint_restore */')
        out.add(f'`endif')            
        out.write_to_file(f'{self.output}.sv')
  
    def generateInterface(self):
        out = svOutputHelper()
        out.add(f'`ifndef {self.output.name.upper()}_IF__SV')
        out.add(f'`define {self.output.name.upper()}_IF__SV')

        out.add(f'`include "{self.output.name}.svh"')

        out.add(f'interface {self.output.name}_if();', '>+1')

        for signal in self.signals:
            msb = f'{signal.width_name}-1' if signal.width_name else f'{signal.width-1}'
            if signal.enum_type is None:
                bitrange = f'[{msb}:0] ' if (signal.width > 1 or signal.width_name) else ''
                out.add(f'{signal.type:5} {bitrange}{signal.name};')
            else:
                out.add(f'`REGSCRIBE_ENUM_TYPE({signal.enum_type}, {"wire" if signal.type.startswith("wire") else "var"}, {msb}) {signal.name};')
        
        
        out.add(f'modport regmap(', '>+1')
        for signal in self.signals:
            out.add(f'{"input" if signal.input else "output":6} {signal.name},')
        out.filter_last(',')
        out.add(f');', '<-1')
        


        out.add(f'`include "{self.output.name}_modports.svh"')
        out.add(f'endinterface\n', '<-1')

        out.add(f'`endif')
        out.write_to_file(f'{self.output}_if.sv')
class svOutputHelper(object):
    def __init__(self):
        self.texts = dict()
        self.indents = dict()
        
    def set_indent(self, indent, container = 'default'):
        num = int(list(filter(str.isdigit, indent))[0])
        
        if '+' in indent:
            self.indents[container] += num
        elif '-' in indent:
            self.indents[container] -= num
        elif '=' in indent:
            self.indents[container] = num
        
    def add(self, text, indent='>+0', container = 'default', pre=None, post=None, start=''):
        if type(container) is str:
            container = {container}
        
        for c in container:        
            if c not in self.texts:
                self.texts[c] = ''
                self.indents[c] = self.indents.get('default', 0)
                
            if '<' in indent:
                self.set_indent(indent, c)
                
            if pre is None:
                pre = f''.rjust(self.indents[c]*4)
                
            if post is None:
                post = '\n'
                        
            self.texts[c] += start + pre + text + post
    
            if '>' in indent:
                self.set_indent(indent, c)


    def merge(self, container, outContainer = 'default'):
        if type(container) is str:
            container = {container}
        
        for c in container:        
            self.indents[outContainer] = self.indents[c]
            self.texts[outContainer] += self.texts[c]
            del self.indents[c]
            del self.texts[c]

    def filter_last(self, symbol, container = 'default', replace = ''):
        if type(container) is str:
            container = {container}
        
        for c in container:
            self.texts[c] = re.sub(f'{symbol}(?!.*//)', replace, self.texts[c][::-1], count=1)[::-1]


    def write_to_file(self, name, container = 'default'):
        with open(name, 'w', encoding="utf-8") as f:
            f.write(self.texts[container])
class svSignal(object):
    def __init__(self, name, type, width=1, input=True, enum_type=None, width_name=None):
        self.name = name
        self.type = type
        self.width = width
        self.input = input
        self.enum_type = enum_type
        self.width_name = width_name
