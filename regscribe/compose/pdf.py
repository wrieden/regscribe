import argparse
import html
import json
import logging
import math
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import reduce
from xml.dom import minidom

import reportlab.pdfgen
import reportlab.pdfgen.canvas
import requests

import reportlab
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.lib import colors

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Flowable, Spacer, Frame
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.pdfgen.pathobject import PDFPathObject


# import xml.etree.ElementTree as ET
from lxml import etree as ET

from regscribe.converter import *

# Create local logger
logger = logging.getLogger(__name__)


def get_composer():
    return compose_pdf()


class compose_pdf(Composer):
    def __init__(self):
        pass

    def get_argparse(self):
        argparser = argparse.ArgumentParser(add_help=False)
        group = argparser.add_argument_group("PDF Builder Arguments")
        group.add_argument("--table_width", type=int, help="Width in inches of the resulting tables")
        group.add_argument("--no_overview", action="store_true", help="Do not generate a register overview")
        group.add_argument("--tm01_specific", action="store_true", help="Use TM01 specific workarounds...")
        group.add_argument("--hier_name_depth", type=int, default=1, help="Name hierachy depth")
        group.add_argument("-o", "--output", type=Path, default="out.docx", help="Output xml file")
        return argparser

    def set_args(self, args):
        self.output_filename: Path = args.output
        self.table_width = args.table_width
        self.no_overview = args.no_overview
        self.tm01_specific = args.tm01_specific
        self.hier_name_depth = args.hier_name_depth

    def compose(self, project):
        logger.info(f"Creating PDF file")

        self.project = project
        regs = self.project.get_children(-1, Register)

        # # doc = docx.Document()

        # # doc.save(self.output_filename)

        # pdf = reportlab.pdfgen.canvas.Canvas(self.output_filename)

        # # setting the title of the document
        # pdf.setTitle("RegMap")

        # # # registering a external font in python
        # # pdfmetrics.registerFont(
        # #     TTFont('abc', 'SakBunderan.ttf')
        # # )

        # # creating the title by setting it's font
        # # and putting it on the canvas
        # pdf.setFont("Helvetica", 36)
        # pdf.drawCentredString(300, 770, "title")

        # # creating the subtitle by setting it's font,
        # # colour and putting it on the canvas
        # pdf.setFillColorRGB(0, 0, 255)
        # pdf.setFont("Courier-Bold", 24)
        # pdf.drawCentredString(290, 720, "subTitle")

        # # drawing a line
        # pdf.line(30, 710, 550, 710)

        # # creating a multiline text using
        # # textline and for loop
        # text = pdf.beginText(40, 680)
        # text.setFont("Courier", 18)
        # text.setFillColor(colors.red)
        # for line in ["hi ho you fella", "tell me how are you"]:
        #     text.textLine(line)
        # pdf.drawText(text)

        # for reg in regs:
        #     pdf.setFont("Helvetica", 16)
        #     pdf.drawText()

        class DrawNextToWord(Flowable):
            def __init__(self, width=50, height=20):
                Flowable.__init__(self)
                self.width = width
                self.height = height

            def draw(self):
                c = self.canv
                c.setStrokeColorRGB(0, 0, 0)
                c.setFillColorRGB(0.75, 0.75, 0.75)
                c.rect(0, 0, self.width, self.height, fill=1)
                c.setFillColorRGB(0, 0, 0)
                c.drawString(5, 5, "Drawing")

        class DrawRegister(Flowable):
            def __init__(self, reg: Register):
                Flowable.__init__(self)
                self.reg: Register = reg

            def draw(self):
                c = self.canv
                c.setStrokeColorRGB(0, 0, 0)

                p: PDFPathObject = c.beginPath()

                x_start = -70
                x_end = -10
                x_width = x_end - x_start

                y_start = 0
                y_end = 10
                y_width = y_end - y_start

                p.moveTo(x_start, y_end)
                p.lineTo(x_start, y_start)
                p.lineTo(x_end, y_start)
                p.lineTo(x_end, y_end)

                f: Field = self.reg.get_children()[0]

                for i in range(1, 32, 1):

                    x = x_end - ((x_width / 32) * i)
                    p.moveTo(x, y_start)

                    if f is None or i != f.offset + f.width:
                        p.lineTo(x, y_start + y_width * 0.33)
                    else:
                        f = f.get_next_child_node()
                        p.lineTo(x, y_start + y_width * 0.66)

                c.drawPath(p)

                c.setFontSize(10)
                c.drawCentredString((x_start + x_end) / 2, y_start + 10, f"0x{self.reg.address:04X}")

                # c.lines([(0, 0, 10, 10)])
                # c.line(self.start_x, self.start_y, self.end_x, self.end_y)

        class DrawFieldToReg(Flowable):
            def __init__(self, field: Field, reg_draw: DrawRegister):
                Flowable.__init__(self)
                self.field: Field = field
                self.reg_draw = reg_draw

            def draw(self):
                c = self.canv
                c.setStrokeColorRGB(0, 0, 0)

                p: PDFPathObject = c.beginPath()

                x_start = -70
                x_end = -10
                x_width = x_end - x_start

                y_start = 0
                y_end = 10
                y_width = y_end - y_start

                x = x_end - ((x_width / 32) * (self.field.offset + self.field.width / 2))

                p.moveTo(0, 5)
                p.lineTo(x, 5)
                # p.lineTo(x, self.reg_draw)

                c.drawPath(p)

                # c.setFontSize(10)
                # c.drawCentredString((x_start + x_end) / 2, y_start + 10, f"0x{self.reg.address:04X}")

                # c.lines([(0, 0, 10, 10)])
                # c.line(self.start_x, self.start_y, self.end_x, self.end_y)

        self.output_filename.parents[0].mkdir(parents=True, exist_ok=True)
        doc = SimpleDocTemplate(self.output_filename, pagesize=letter)
        styles = getSampleStyleSheet()
        elements = []

        # Adding a paragraph
        text = "This is an overview of the register. Below is the table with the data."
        paragraph = Paragraph(text, styles["Normal"])
        elements.append(paragraph)

        for reg in regs:
            # elements.append(Paragraph(f"0x{reg.address:04X}: {reg.get_name()}\n", styles["h3"]))
            elements.append(Paragraph(f"{reg.get_name()}\n", styles["h3"]))
            line = DrawRegister(reg)
            elements.append(line)
            # drawing = DrawNextToWord()
            # elements.append(drawing)
            elements.append(Paragraph(f"{reg.get_description()}\n", styles["Normal"]))

            for field in reg.get_children():
                elements.append(
                    Paragraph(
                        (f"[{field.offset+field.width-1}:" if field.width > 1 else "[") + f"{field.offset}] " + field.get_name() + f"\n", styles["Normal"]
                    )
                )
                elements.append(DrawFieldToReg(field, line))
                elements.append(Paragraph(f"{field.get_description()}\n", styles["Normal"]))

        # Adding a table
        data = [["Header1", "Header2", "Header3"]] + [["Row{}".format(i), "Data{}".format(i * 2 - 1), "Data{}".format(i * 2)] for i in range(1, 101)]
        table = Table(data)
        style = TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.transparent),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
                ("BACKGROUND", (0, 1), (-1, -1), colors.beige),
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ]
        )
        table.setStyle(style)

        # Splitting the table across pages
        for split_table in table.split(doc.width, doc.height):
            elements.append(split_table)

        frame = Frame(0, 0, 60, 50, showBoundary=1)

        doc.build(elements)
        # saving the pdf
        # pdf.save()
