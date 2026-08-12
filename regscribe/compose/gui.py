import argparse
import threading
import time
from collections import deque

from regscribe.converter import Access, Choice, Composer, Encoding, Field, Log, Project, Register, Visibility

try:
    import dearpygui.dearpygui as dpg
except ImportError:
    dpg = None


MONITOR_TASK = "gui"
SCOPE_TASK = "scope"
FAVORITE_TASK = "favorite"
FIELD_PAYLOAD = "field"

VISIBILITY_RANK = {Visibility.PUBLIC: 0, Visibility.INTERNAL: 1, Visibility.PRIVATE: 2, Visibility.HIDDEN: 3}

TRACE_COLORS = [
    (255, 209, 102),
    (6, 214, 160),
    (239, 71, 111),
    (17, 138, 178),
    (192, 132, 252),
    (255, 145, 77),
    (144, 224, 239),
    (163, 230, 53),
]

TRIGGER_MODES = ["free run", "rising edge", "falling edge", "above level", "below level", "equal"]

DEFAULT_SAMPLES = 1000

BAR_HEIGHT = 19
BAR_BACK = (37, 42, 52)
BAR_EDGE = (70, 76, 88)
BAR_FILL = (60, 130, 200)
DRAG_THRESHOLD = 3
ZONE_BAND = 0.25
ZONE_ADD = (60, 130, 200, 45)
ZONE_SPLIT = (90, 200, 120, 80)


def get_composer():
    return compose_gui()


def sign_extend(raw: int, bits: int) -> int:
    return raw - (1 << bits) if raw & (1 << (bits - 1)) else raw


def field_raw(field: Field, word: int) -> int:
    return (word & field.mask) >> field.offset


def choice_map(field: Field) -> dict[int, str]:
    return {choice.offset: choice.get_name() for choice in field.get_children(child_type=Choice)}


def decode(field: Field, raw: int):
    """Raw field bits to the physical value described by encoding, exponent and mantissa."""
    encoding = field.encoding or Encoding.UNSIGNED
    exponent = field.exponent or 0

    if encoding == Encoding.CHOICE:
        return choice_map(field).get(raw, raw)

    if encoding == Encoding.ASCII:
        chars = [(raw >> shift) & 0xFF for shift in range(0, field.width, 8)]
        return "".join(chr(c) if 32 <= c < 127 else "." for c in chars)

    if encoding in (Encoding.FLOAT, Encoding.UFLOAT):
        mantissa_bits = min(field.mantissa or field.width, field.width)
        mantissa = raw & ((1 << mantissa_bits) - 1)
        if encoding == Encoding.FLOAT:
            mantissa = sign_extend(mantissa, mantissa_bits)
        return mantissa * 2.0 ** ((raw >> mantissa_bits) + exponent)

    value = sign_extend(raw, field.width) if encoding.signed() else raw
    return value * 2.0 ** exponent if exponent else value


def encode(field: Field, value) -> int:
    """Inverse of decode for the encodings the value widgets allow editing."""
    exponent = field.exponent or 0
    if field.encoding == Encoding.ASCII:
        raw = 0
        for index, char in enumerate(str(value)[: field.width // 8]):
            raw |= (ord(char) & 0xFF) << (8 * index)
        return raw
    raw = round(float(value) / 2.0 ** exponent) if exponent else int(value)
    return raw & ((1 << field.width) - 1)


def numeric(field: Field, raw: int) -> float:
    """Plottable value, choices and strings stay on their raw code."""
    value = decode(field, raw)
    return float(value) if isinstance(value, (int, float)) else float(raw)


def bit_range(field: Field) -> str:
    return f"[{field.lsb}]" if field.width == 1 else f"[{field.msb}:{field.lsb}]"


def number_text(field: Field) -> str:
    value = decode(field, field.value)
    return f"{value:.6g}" if isinstance(value, float) and not value.is_integer() else f"{value}"


def value_text(field: Field) -> str:
    text = number_text(field)
    return f"{text} {field.unit}".strip() if field.unit else text


def focused(item) -> bool:
    """A widget that is being edited or dragged must not be overwritten by incoming values."""
    state = dpg.get_item_state(item)
    return bool(state.get("focused") or state.get("active"))


class Trace:
    def __init__(self, field: Field, color, depth: int):
        self.field = field
        self.register: Register = field.parent
        self.color = color
        self.label = field.get_hier_name(depth=3)
        self.time = deque(maxlen=depth)
        self.value = deque(maxlen=depth)
        self.last = None
        self.series = None
        self.chip = None
        self.pane = None
        self.dirty = True

    def append(self, t: float, value: float):
        self.last = self.value[-1] if self.value else None
        self.time.append(t)
        self.value.append(value)
        self.dirty = True

    def period(self) -> float:
        if len(self.time) < 2:
            return 0.001
        window = min(len(self.time), 50)
        span = self.time[-1] - self.time[-window]
        return span / (window - 1) if span > 0 else 0.001

    def range(self) -> tuple[float, float]:
        scale = 2.0 ** (self.field.exponent or 0)
        low, high = (self.field.min or 0) * scale, (self.field.max or 0) * scale
        return (low, high) if high > low else (0.0, 2.0 ** self.field.width - 1)


class Favorite:
    def __init__(self, node: Field, row: int, widgets: list):
        self.node = node
        self.register: Register = node.parent
        self.row = row
        self.widgets = widgets
        self.shown = None


class Pane:
    def __init__(self, holder: int, plot: int, x_axis: int, y_axis: int, cursor: int, trigger: int):
        self.holder = holder
        self.plot = plot
        self.x_axis = x_axis
        self.y_axis = y_axis
        self.cursor = cursor
        self.trigger = trigger
        self.traces: list[Trace] = list()
        self.locked = False
        self.y_limits = None
        self.fit_time = 0.0


class Scope:
    IDLE, ARMED, TRIGGERED, HELD = "idle", "armed", "triggered", "held"

    def __init__(self, depth: int):
        self.depth = depth
        self.traces: dict[Field, Trace] = dict()
        self.mode = TRIGGER_MODES[0]
        self.source: Trace | None = None
        self.level = 0.0
        self.single = False
        self.state = self.ARMED
        self.trigger_time = None
        self.capture_time = None
        self.captured = False
        self.reference = 0.0
        self.period = 0.001
        self.view = None

    def add(self, field: Field) -> Trace:
        used = {trace.color for trace in self.traces.values()}
        color = next((c for c in TRACE_COLORS if c not in used), TRACE_COLORS[len(self.traces) % len(TRACE_COLORS)])
        trace = Trace(field, color, self.depth)
        self.traces[field] = trace
        if self.source is None:
            self.source = trace
        return trace

    def remove(self, field: Field) -> Trace | None:
        trace = self.traces.pop(field, None)
        if trace is not None and self.source is trace:
            self.source = next(iter(self.traces.values()), None)
            if self.source is None:
                self.mode = TRIGGER_MODES[0]
        return trace

    def arm(self, single=False):
        self.single = single
        self.state = self.ARMED

    def stop(self):
        self.state = self.IDLE

    def force(self):
        self.trigger_time = self.source.time[-1] if self.source and self.source.time else time.time()
        self.state = self.TRIGGERED

    def default_view(self) -> tuple[float, float]:
        span = DEFAULT_SAMPLES * self.period
        return (-span, 0.0) if self.mode == TRIGGER_MODES[0] else (-0.2 * span, 0.8 * span)

    def limits(self) -> tuple[float, float]:
        return self.view if self.view is not None else self.default_view()

    def spans(self) -> tuple[float, float]:
        low, high = self.limits()
        return max(0.0, -low), max(0.0, high)

    def samples(self) -> tuple[int, int]:
        pre, post = self.spans()
        return round(pre / self.period), round(post / self.period)

    def update_period(self):
        """Latch the sample period, a per frame estimate would make the readout jitter."""
        period = self.source.period() if self.source else 0.001
        if abs(period - self.period) > 0.2 * self.period:
            self.period = period

    def ready(self) -> bool:
        if self.source is None:
            return False
        low, high = self.limits()
        return len(self.source.time) >= min(round((high - low) / self.period), self.depth)

    def fires(self, trace: Trace, value: float) -> bool:
        previous = trace.last
        if self.mode == "rising edge":
            return previous is not None and previous < self.level <= value
        if self.mode == "falling edge":
            return previous is not None and previous > self.level >= value
        if self.mode == "above level":
            return value > self.level
        if self.mode == "below level":
            return value < self.level
        if self.mode == "equal":
            return value == self.level
        return False

    def feed(self, trace: Trace, t: float, value: float):
        if self.state in (self.HELD, self.IDLE):
            return
        trace.append(t, value)

        if trace is not self.source or self.mode == TRIGGER_MODES[0]:
            return

        if self.state == self.ARMED and self.fires(trace, value):
            self.trigger_time = t
            self.state = self.TRIGGERED
        elif self.state == self.TRIGGERED and t - self.trigger_time >= self.spans()[1]:
            self.capture_time = self.trigger_time
            self.captured = True
            self.state = self.HELD if self.single else self.ARMED

    def take_capture(self) -> bool:
        """A triggered capture is only shown once complete, otherwise the curve would creep in."""
        if self.mode == TRIGGER_MODES[0]:
            return True
        complete, self.captured = self.captured, False
        return complete

    def update_reference(self):
        if self.mode != TRIGGER_MODES[0]:
            if self.capture_time is not None:
                self.reference = self.capture_time
        elif self.source and self.source.time:
            self.reference = self.source.time[-1]


class compose_gui(Composer):
    def __init__(self):
        self.comm_type = "dummy"
        self.baudrate = 115200
        self.depth = 10000
        self.rate = 1000
        self.visibility = Visibility.INTERNAL

    def get_argparse(self):
        argparser = argparse.ArgumentParser(add_help=False)
        group = argparser.add_argument_group("GUI Arguments")
        group.add_argument("--comm", choices=["uart", "dummy", "none"], default="dummy", help="Selects the communication method, none browses the register map offline")
        group.add_argument("--baudrate", type=int, default=115200, help="Baudrate of the uart communication")
        group.add_argument("--rate", type=int, default=1000, help="Responses per second of the dummy device")
        group.add_argument("--depth", type=int, default=10000, help="Number of samples kept per scope trace")
        group.add_argument("--visibility", choices=[v.value for v in Visibility], default=Visibility.INTERNAL.value, help="Highest visibility level that is still shown")
        return argparser

    def set_args(self, args):
        self.comm_type = args.comm
        self.baudrate = args.baudrate
        self.rate = args.rate
        self.depth = args.depth
        self.visibility = Visibility(args.visibility)

    def compose(self, project: Project):
        if dpg is None:
            Log.fatal("The gui composer needs dearpygui, install it with 'pip install dearpygui'")
        app = RegisterGui(project, self)
        app.run()


class RegisterGui:
    def __init__(self, project: Project, options: compose_gui):
        self.project = project
        self.options = options
        self.comm = None
        self.scope = Scope(options.depth)

        self.selected: Register | None = None
        self.field_widgets: list[tuple[Field, int, str]] = list()
        self.matches: list[tuple[int, str, Register, tuple | None]] = list()
        self.favorites: list[Favorite] = list()
        self.pin_boxes: dict[Field, list[int]] = dict()
        self.register_nodes: dict[Register, int] = dict()
        self.open_nodes: dict[int, bool] = dict()
        self.bars: dict[int, dict] = dict()
        self.drag: dict | None = None
        self.entry: dict | None = None
        self.tree_rows: dict[Register, list[tuple[Field, int, str]]] = dict()
        self.shown_rows: dict[Register, list[tuple[Field, int, str]]] = dict()
        self.left_values: dict[Register, int] = dict()
        self.shown_value = None
        self.monitored: dict[Register, str] = dict()

        self.updates = 0
        self.update_rate = 0.0
        self.rate_time = time.perf_counter()

        self.plotted = False
        self.rows: list[dict] = list()
        self.layout_dirty = True
        self.row_fields: dict[int, Field] = dict()
        self.dragged: Field | None = None
        self.mouse_down = False

        self.items = dict()

    # connection

    def connect(self):
        if self.options.comm_type == "uart":
            from regscribe.comm.uart import comm_uart

            self.comm = comm_uart(self.project, baudrate=self.options.baudrate)
        elif self.options.comm_type == "dummy":
            from regscribe.comm.dummy import comm_dummy

            self.comm = comm_dummy(self.project, rate=self.options.rate)

        if self.comm is not None:
            self.comm.connect(False)

    def disconnect(self):
        if self.comm is not None:
            try:
                self.comm.disconnect()
            except Exception as error:
                Log.warn(f"Failed to disconnect: {error}")
            self.comm = None

    def background(self, function, *args):
        """Device access blocks and may time out, so keep it off the render thread."""

        def worker():
            try:
                function(*args)
            except BaseException as error:
                Log.error(f"Device access failed: {error}")

        threading.Thread(target=worker, daemon=True).start()

    def visible(self, node) -> bool:
        return VISIBILITY_RANK.get(node.visibility, 0) <= VISIBILITY_RANK[self.options.visibility]

    # window setup

    def run(self):
        self.connect()
        dpg.create_context()
        dpg.create_viewport(title=f"regscribe - {self.project.get_name()}", width=1600, height=1000)
        self.build()
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_viewport_resize_callback(self.on_resize)
        dpg.set_primary_window(self.items["root"], True)
        try:
            while dpg.is_dearpygui_running():
                self.frame()
                dpg.render_dearpygui_frame()
        finally:
            dpg.destroy_context()
            self.disconnect()

    def build(self):
        # a drag only starts from an item that goes active while held, which rules out text and selectables
        with dpg.theme() as row_theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (70, 90, 120, 130))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (80, 110, 150, 170))
                dpg.add_theme_style(dpg.mvStyleVar_ButtonTextAlign, 0.0, 0.5)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 2, 0)
        self.items["row_theme"] = row_theme

        with dpg.window() as root:
            self.items["root"] = root
            self.items["status"] = dpg.add_text("")
            dpg.add_separator()
            with dpg.group(horizontal=True):
                with dpg.child_window(width=420):
                    self.build_tree()
                with dpg.group():
                    self.items["detail"] = dpg.add_child_window(height=-580)
                    with dpg.child_window(height=220):
                        dpg.add_text("favorites", color=(120, 200, 255))
                        dpg.add_separator()
                        self.items["favorites"] = dpg.add_group()
                    with dpg.child_window(height=-1) as scope:
                        self.items["scope"] = scope
                        self.build_scope()

        self.items["overlay"] = dpg.add_viewport_drawlist(front=True, show=False)
        self.items["zone"] = dpg.draw_rectangle((0, 0), (0, 0), parent=self.items["overlay"], fill=ZONE_ADD, color=BAR_FILL, thickness=2)
        self.items["zone_text"] = dpg.draw_text((0, 0), "", size=15, parent=self.items["overlay"])
        self.show_register(None)

    def build_tree(self):
        dpg.add_input_text(hint="filter registers and fields", width=-1, callback=self.on_filter)
        dpg.add_separator()
        self.items["tree"] = dpg.add_group()
        self.items["flat"] = dpg.add_group(show=False)

        # without the flat frame padding a pin checkbox would make its row taller than a tree node
        with dpg.theme() as rows:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 2, 0)
        dpg.bind_item_theme(self.items["tree"], rows)
        dpg.bind_item_theme(self.items["flat"], rows)

        for child in self.project.get_children():
            self.build_tree_node(child, self.items["tree"])

        for register in self.project.get_children(-1, Register):
            if not self.visible(register):
                continue
            name = register.get_hier_name()
            self.build_flat_row(register, None, f"{name}  0x{register.address:04X}", name)
            for field in register.get_children(child_type=Field):
                if self.visible(field):
                    self.build_flat_row(register, field, f"{name}/{field.get_name()}  {bit_range(field)}", f"{name}/{field.get_name()}")

    def build_flat_row(self, register: Register, field: Field | None, label: str, key: str):
        with dpg.group(horizontal=True, parent=self.items["flat"]) as row:
            if field is not None:
                self.add_pin_box(field)
            item = self.add_row_button(label, register, field)
        entry = None if field is None else (field, item, label)
        self.matches.append((row, key.lower(), register, entry))

    def build_tree_node(self, node, parent):
        if not self.visible(node):
            return
        if isinstance(node, Register):
            self.build_register_node(node, parent)
            return
        with dpg.tree_node(label=node.get_name(), parent=parent) as branch:
            for child in node.get_children():
                self.build_tree_node(child, branch)

    def build_register_node(self, register: Register, parent):
        rows = []
        with dpg.tree_node(label=f"{register.get_name()}  0x{register.address:04X}", parent=parent) as node:
            for field in register.get_children(child_type=Field):
                if self.visible(field):
                    with dpg.group(horizontal=True):
                        self.add_pin_box(field)
                        label = f"{field.get_name()}  {bit_range(field)}"
                        rows.append((field, self.add_row_button(label, register, field), label))

        self.register_nodes[register] = node
        self.open_nodes[node] = False
        self.tree_rows[register] = rows

    def add_pin_box(self, field: Field) -> int:
        item = dpg.add_checkbox(default_value=any(f.node is field for f in self.favorites), callback=self.on_pin_box, user_data=field)
        with dpg.tooltip(item):
            dpg.add_text("keep in favorites")
        # the detail table is rebuilt on every selection, so drop the boxes it left behind
        self.pin_boxes[field] = [i for i in self.pin_boxes.get(field, ()) if dpg.does_item_exist(i)] + [item]
        return item

    def add_row_button(self, label: str, register: Register, field: Field | None = None, width: int = -1) -> int:
        item = dpg.add_button(label=label, width=width, callback=self.on_select, user_data=register)
        dpg.bind_item_theme(item, self.items["row_theme"])
        if field is not None:
            self.row_fields[item] = field
            with dpg.drag_payload(parent=item, drag_data=field, payload_type=FIELD_PAYLOAD):
                dpg.add_text(f"plot {field.get_hier_name(depth=3)}")
        return item

    def build_scope(self):
        with dpg.group(horizontal=True):
            dpg.add_button(label="Run", callback=lambda: self.scope.arm(single=False))
            dpg.add_button(label="Single", callback=lambda: self.scope.arm(single=True))
            dpg.add_button(label="Stop", callback=self.scope.stop)
            dpg.add_button(label="Force", callback=self.scope.force)
            dpg.add_button(label="Clear", callback=self.on_clear_traces)
            dpg.add_text("source")
            self.items["source"] = dpg.add_combo(items=[], width=180, callback=self.on_source)
            dpg.add_text("trigger")
            self.items["mode"] = dpg.add_combo(items=TRIGGER_MODES, default_value=self.scope.mode, width=110, callback=self.on_mode)
            self.items["level"] = dpg.add_input_float(default_value=0.0, width=90, step=0, callback=self.on_level)
            self.items["choice_level"] = dpg.add_combo(items=[], width=90, show=False, callback=self.on_choice_level)
            dpg.add_button(label="Fit", callback=self.on_fit_window)
            self.items["autofit"] = dpg.add_checkbox(label="auto y", default_value=True)
            self.items["normalize"] = dpg.add_checkbox(label="normalize", callback=self.on_normalize)
            self.items["window"] = dpg.add_text("")

        self.items["traces"] = dpg.add_group(horizontal=True)
        self.items["hint"] = dpg.add_text("drag a field onto a plot to trace it, drop near an edge for a new plot", parent=self.items["traces"], color=(130, 130, 140))

        self.items["layout"] = dpg.add_group()
        self.add_pane()

    def add_pane(self, row: dict | None = None, before: int = 0) -> Pane:
        if row is None:
            row = {"group": dpg.add_group(horizontal=True, parent=self.items["layout"]), "panes": []}
            self.rows.append(row)

        with dpg.group(parent=row["group"], before=before) as holder:
            with dpg.plot(width=400, height=240, anti_aliased=True,
                          payload_type=FIELD_PAYLOAD, drop_callback=self.on_drop) as plot:
                dpg.add_plot_legend(payload_type=FIELD_PAYLOAD, drop_callback=self.on_drop)
                x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="t [s]")
                y_axis = dpg.add_plot_axis(dpg.mvYAxis, payload_type=FIELD_PAYLOAD, drop_callback=self.on_drop)
                cursor = dpg.add_drag_line(color=(255, 255, 255, 90), default_value=0.0)
                trigger = dpg.add_drag_line(color=(255, 80, 80, 120), vertical=False, default_value=0.0, show=False, callback=self.on_drag_level)

        pane = Pane(holder, plot, x_axis, y_axis, cursor, trigger)
        position = next((i for i, p in enumerate(row["panes"]) if p.holder == before), len(row["panes"]))
        row["panes"].insert(position, pane)
        self.layout_dirty = True
        return pane

    def remove_pane(self, pane: Pane):
        if sum(len(row["panes"]) for row in self.rows) < 2:
            return
        for index, row in enumerate(self.rows):
            if pane in row["panes"]:
                row["panes"].remove(pane)
                dpg.delete_item(pane.holder)
                if not row["panes"]:
                    dpg.delete_item(row["group"])
                    self.rows.pop(index)
                break
        self.layout_dirty = True

    def panes(self) -> list[Pane]:
        return [pane for row in self.rows for pane in row["panes"]]

    def relayout(self):
        width, height = dpg.get_item_rect_size(self.items["scope"])
        if width < 100 or height < 100:
            return
        self.layout_dirty = False
        rows = max(1, len(self.rows))
        for row in self.rows:
            columns = max(1, len(row["panes"]))
            for pane in row["panes"]:
                dpg.configure_item(pane.plot, width=int((width - 16) / columns) - 8, height=int((height - 70) / rows) - 8)

    def split(self, pane: Pane, zone: str) -> Pane:
        row = next(r for r in self.rows if pane in r["panes"])
        if zone in ("left", "right"):
            after = row["panes"][row["panes"].index(pane) + 1:]
            before = pane.holder if zone == "left" else (after[0].holder if after else 0)
            return self.add_pane(row, before)

        index = self.rows.index(row) + (0 if zone == "top" else 1)
        following = self.rows[index] if index < len(self.rows) else None
        group = dpg.add_group(horizontal=True, parent=self.items["layout"], before=following["group"] if following else 0)
        new_row = {"group": group, "panes": []}
        self.rows.insert(index, new_row)
        return self.add_pane(new_row)
    # register detail

    def show_register(self, register: Register | None):
        previous = self.register_nodes.get(self.selected)
        node = self.register_nodes.get(register)
        if previous is not None and previous != node:
            dpg.set_value(previous, False)
            self.open_nodes[previous] = False
        if node is not None:
            dpg.set_value(node, True)
            self.open_nodes[node] = True

        self.selected = register
        self.shown_value = None
        self.field_widgets.clear()
        dpg.delete_item(self.items["detail"], children_only=True)
        if register is None:
            dpg.add_text("select a register in the tree", parent=self.items["detail"])
            return

        parent = self.items["detail"]
        dpg.add_text(register.get_hier_name(), parent=parent, color=(120, 200, 255))
        dpg.add_text(f"address 0x{register.address:04X}   width {register.width}   status {register.status.value}", parent=parent)
        description = register.get_description()
        if description:
            dpg.add_text(description, parent=parent, wrap=900)

        with dpg.group(horizontal=True, parent=parent):
            self.items["raw"] = dpg.add_input_text(default_value=f"{register.value:08X}", hexadecimal=True, width=140, on_enter=True, callback=self.on_raw)
            dpg.add_button(label="Read", callback=self.on_read, enabled=self.comm is not None)
            dpg.add_button(label="Write", callback=self.on_write, enabled=self.comm is not None)
            dpg.add_button(label="Reset", callback=self.on_reset)
            dpg.add_checkbox(label="monitor", default_value=register in self.monitored, callback=self.on_monitor, enabled=self.comm is not None)
            self.items["priority"] = dpg.add_input_int(default_value=1, width=110, min_value=1, min_clamped=True, step=0, callback=self.on_monitor)

        with dpg.table(parent=parent, header_row=True, resizable=True, borders_innerH=True, borders_outerH=True, borders_innerV=True, borders_outerV=True, policy=dpg.mvTable_SizingStretchProp):
            for label, width in (("field", 3), ("bits", 1), ("access", 1), ("reset", 1), ("raw", 1), ("value", 3), ("unit", 1), ("pin", 1)):
                dpg.add_table_column(label=label, init_width_or_weight=width)

            for field in register.get_children(child_type=Field):
                if self.visible(field):
                    self.build_field_row(field)

    def build_field_row(self, field: Field):
        access = field.access or Access.RW
        with dpg.table_row():
            name = self.add_row_button(field.get_name(), field.parent, field)
            if field.get_description():
                with dpg.tooltip(name):
                    dpg.add_text(field.get_description(), wrap=500)
            dpg.add_text(bit_range(field))
            dpg.add_text(access.value or "-")
            dpg.add_text(f"0x{field.reset_value:X}" if field.reset_value is not None else "-")
            raw_text = dpg.add_text(f"0x{field.value:X}")
            widget, kind = self.build_field_widget(field, access)
            dpg.add_text(field.unit or "")
            self.add_pin_box(field)

        self.field_widgets.append((field, raw_text, "raw"))
        self.field_widgets.append((field, widget, kind))

    def build_field_widget(self, field: Field, access: Access):
        raw = field.value
        writable = access.can_write()
        encoding = field.encoding or Encoding.UNSIGNED
        scale = 2.0 ** (field.exponent or 0)
        low, high = (field.min or 0), (field.max if field.max is not None else (1 << field.width) - 1)

        if encoding == Encoding.CHOICE and field.get_children(child_type=Choice):
            names = list(choice_map(field).values())
            widget = dpg.add_combo(items=names, default_value=choice_map(field).get(raw, f"{raw}"), width=-1, callback=self.on_field, user_data=field)
            kind = "choice"
        elif field.width == 1:
            widget = dpg.add_checkbox(default_value=bool(raw), callback=self.on_field, user_data=field)
            kind = "bool"
        elif encoding == Encoding.ASCII:
            widget = dpg.add_input_text(default_value=decode(field, raw), width=-1, on_enter=True, callback=self.on_field, user_data=field)
            kind = "text"
        elif encoding in (Encoding.FLOAT, Encoding.UFLOAT):
            widget = self.build_bar(field, "rawint", 0, (1 << field.width) - 1, writable)
            kind = "bar"
        elif high <= low:
            widget = dpg.add_input_int(default_value=int(decode(field, raw)), width=-1, step=1, callback=self.on_field, user_data=field)
            kind = "int"
        elif field.exponent:
            widget = self.build_bar(field, "float", low * scale, high * scale, writable)
            kind = "bar"
        else:
            widget = self.build_bar(field, "int", low, high, writable)
            kind = "bar"

        if kind != "bar":
            if not writable:
                dpg.configure_item(widget, enabled=False)
            dpg.configure_item(widget, user_data=(field, kind))
        return widget, kind

    def build_bar(self, field: Field, edit: str, low: float, high: float, writable: bool) -> int:
        """A drawn bar, dragging it sets the value and a plain click opens the entry next to it."""
        with dpg.group(horizontal=True):
            with dpg.drawlist(width=-1, height=BAR_HEIGHT) as bar:
                back = dpg.draw_rectangle((0, 0), (10, BAR_HEIGHT), fill=BAR_BACK, color=BAR_EDGE)
                fill = dpg.draw_rectangle((0, 0), (0, BAR_HEIGHT), fill=BAR_FILL, color=BAR_FILL)
                text = dpg.draw_text((6, 3), "", size=13)
            if edit == "float":
                entry = dpg.add_input_float(width=-1, show=False, on_enter=True, callback=self.on_entry, user_data=(field, edit))
            else:
                entry = dpg.add_input_int(width=-1, show=False, step=0, on_enter=True, callback=self.on_entry, user_data=(field, edit))

        self.bars[bar] = {
            "field": field, "edit": edit, "low": low, "high": high, "writable": writable,
            "back": back, "fill": fill, "text": text, "entry": entry, "width": 0,
        }
        return bar

    def bar_value(self, info: dict) -> float:
        field = info["field"]
        return field.value if info["edit"] == "rawint" else float(decode(field, field.value))

    def draw_bar(self, bar: int):
        info = self.bars[bar]
        if not info["width"]:
            # the stretched size is only known once the table has laid the cell out
            width = dpg.get_item_rect_size(bar)[0]
            if width < 16:
                return
            info["width"] = width
            # a drawlist left on a stretched width clips everything it draws
            dpg.configure_item(bar, width=width)
            dpg.configure_item(info["back"], pmax=(width, BAR_HEIGHT))

        width = info["width"]

        low, high = info["low"], info["high"]
        span = (self.bar_value(info) - low) / (high - low) if high > low else 0.0
        dpg.configure_item(info["fill"], pmax=(max(0.0, min(1.0, span)) * width, BAR_HEIGHT))
        dpg.configure_item(info["text"], text=number_text(info["field"]))

    def widget_value(self, field: Field, kind: str, data) -> int:
        if kind == "choice":
            for value, name in choice_map(field).items():
                if name == data:
                    return value
            return field.value
        if kind == "bool":
            return int(bool(data))
        if kind == "rawint":
            return int(data) & ((1 << field.width) - 1)
        return encode(field, data)

    # callbacks

    def on_filter(self, sender, data):
        """Every whitespace separated word has to appear somewhere in the hierarchical name."""
        words = data.lower().split()
        self.shown_rows = dict()
        for item, name, register, entry in self.matches:
            match = all(word in name for word in words)
            dpg.configure_item(item, show=match)
            if match and words and entry is not None:
                self.shown_rows.setdefault(register, []).append(entry)
        dpg.configure_item(self.items["flat"], show=bool(words))
        dpg.configure_item(self.items["tree"], show=not words)
        self.left_values.clear()

    def on_select(self, sender, data, register):
        self.show_register(register)

    def on_raw(self, sender, data):
        try:
            self.write_register(self.selected, int(str(data), 16))
        except ValueError:
            Log.warn(f"Not a hex value: {data}")

    def on_field(self, sender, data, user_data):
        field, kind = user_data
        self.write_field(field, kind, data)

    def on_entry(self, sender, data, user_data):
        field, edit = user_data
        self.write_field(field, edit, data)
        bar = next((b for b, info in self.bars.items() if info["entry"] == sender), None)
        if bar is not None:
            self.close_entry(bar)

    def write_field(self, field: Field, kind: str, data):
        raw = self.widget_value(field, kind, data)
        register: Register = field.parent
        self.write_register(register, (register.value & ~field.mask) | ((raw << field.offset) & field.mask))

    def on_read(self):
        if self.comm is not None and self.selected is not None:
            self.background(self.selected.read)

    def on_write(self):
        if self.selected is not None:
            self.write_register(self.selected, self.selected.value)

    def on_reset(self):
        if self.selected is not None:
            self.write_register(self.selected, self.selected.reset_value)

    def on_monitor(self, sender, data):
        register = self.selected
        if register is None or self.comm is None:
            return
        priority = max(1, dpg.get_value(self.items["priority"]))
        enabled = data if isinstance(data, bool) else register in self.monitored
        if enabled:
            register.monitor(priority=priority, task=MONITOR_TASK)
            self.monitored[register] = MONITOR_TASK
        else:
            register.stop_monitor(task=MONITOR_TASK)
            self.monitored.pop(register, None)

    def on_pin_box(self, sender, data, node):
        self.pin(node) if data else self.unpin(node)

    def pin(self, field: Field):
        if any(favorite.node is field for favorite in self.favorites):
            return

        register: Register = field.parent
        with dpg.group(horizontal=True, parent=self.items["favorites"]) as row:
            dpg.add_button(label="x", width=22, callback=self.on_unpin, user_data=field)
            self.add_row_button(field.get_hier_name(depth=3), register, field, width=260)
            widget, kind = self.build_field_widget(field, field.access or Access.RW)
            if kind not in ("bool", "bar"):
                dpg.configure_item(widget, width=240)
            widgets = [(field, widget, kind)]
            if field.unit:
                dpg.add_text(field.unit)

        self.favorites.append(Favorite(field, row, widgets))
        self.sync_pin(field, True)
        if self.comm is not None:
            register.monitor(priority=1, task=FAVORITE_TASK)

    def unpin(self, field: Field):
        favorite = next((f for f in self.favorites if f.node is field), None)
        if favorite is None:
            return
        self.favorites.remove(favorite)
        dpg.delete_item(favorite.row)
        self.sync_pin(field, False)
        if self.comm is not None and not any(f.register is favorite.register for f in self.favorites):
            favorite.register.stop_monitor(task=FAVORITE_TASK)

    def sync_pin(self, node, value: bool):
        items = [item for item in self.pin_boxes.get(node, ()) if dpg.does_item_exist(item)]
        self.pin_boxes[node] = items
        for item in items:
            dpg.set_value(item, value)

    def on_unpin(self, sender, data, field: Field):
        self.unpin(field)

    def on_drop(self, sender, field: Field, user_data=None):
        pane = self.pane_of(sender)
        if pane is None:
            return
        zone = self.drop_zone(pane.plot)
        self.add_trace(field, pane if zone == "center" else self.split(pane, zone))

    def drop_zone(self, plot: int) -> str:
        x, y = dpg.get_mouse_pos(local=False)
        left, top = dpg.get_item_rect_min(plot)
        width, height = dpg.get_item_rect_size(plot)
        across, down = (x - left) / max(1, width), (y - top) / max(1, height)
        if not (0.0 <= across <= 1.0 and 0.0 <= down <= 1.0):
            return "center"
        edges = {"left": across, "right": 1.0 - across, "top": down, "bottom": 1.0 - down}
        zone = min(edges, key=edges.get)
        return zone if edges[zone] < ZONE_BAND else "center"

    def zone_rect(self, plot: int, zone: str):
        left, top = dpg.get_item_rect_min(plot)
        width, height = dpg.get_item_rect_size(plot)
        if zone == "left":
            return (left, top), (left + width * ZONE_BAND, top + height)
        if zone == "right":
            return (left + width * (1 - ZONE_BAND), top), (left + width, top + height)
        if zone == "top":
            return (left, top), (left + width, top + height * ZONE_BAND)
        if zone == "bottom":
            return (left, top + height * (1 - ZONE_BAND)), (left + width, top + height)
        return (left, top), (left + width, top + height)

    def pane_of(self, item: int) -> Pane | None:
        """A drop can land on the plot, one of its axes or the legend."""
        panes = {pane.plot: pane for pane in self.panes()}
        while item and item not in panes:
            item = dpg.get_item_parent(item)
        return panes.get(item)

    def add_trace(self, field: Field, pane: Pane | None = None):
        if field in self.scope.traces:
            return
        pane = pane or next(iter(self.panes()))
        trace = self.scope.add(field)
        trace.pane = pane
        pane.traces.append(trace)

        trace.series = dpg.add_line_series([], [], label=trace.label, parent=pane.y_axis)
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvLineSeries):
                dpg.add_theme_color(dpg.mvPlotCol_Line, trace.color, category=dpg.mvThemeCat_Plots)
        dpg.bind_item_theme(trace.series, theme)

        trace.chip = dpg.add_button(label=f"x {trace.label}", parent=self.items["traces"], callback=self.on_remove_trace, user_data=field)
        with dpg.theme() as chip_theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Text, trace.color)
        dpg.bind_item_theme(trace.chip, chip_theme)

        if self.comm is not None:
            field.parent.monitor(priority=1, task=SCOPE_TASK)
        self.plotted = False
        self.refresh_sources()

    def on_remove_trace(self, sender, data, field: Field):
        trace = self.scope.remove(field)
        if trace is None:
            return
        pane = trace.pane
        pane.traces.remove(trace)
        dpg.delete_item(trace.series)
        dpg.delete_item(trace.chip)
        if not pane.traces:
            self.remove_pane(pane)
        if self.comm is not None and not any(t.register is field.parent for t in self.scope.traces.values()):
            field.parent.stop_monitor(task=SCOPE_TASK)
        self.plotted = False
        self.refresh_sources()

    def on_clear_traces(self):
        for field in list(self.scope.traces):
            self.on_remove_trace(None, None, field)

    def on_source(self, sender, data):
        self.scope.source = next((t for t in self.scope.traces.values() if t.label == data), self.scope.source)
        self.refresh_sources()

    def on_mode(self, sender, data):
        self.scope.mode = data
        self.scope.view = None
        self.scope.captured = False
        self.plotted = False
        for trace in self.scope.traces.values():
            dpg.set_value(trace.series, [[], []])
        self.scope.arm(single=self.scope.single)

    def on_level(self, sender, data):
        self.scope.level = float(data)

    def on_drag_level(self, sender, data):
        position = float(dpg.get_value(sender))
        source = self.scope.source
        if dpg.get_value(self.items["normalize"]) and source is not None:
            low, high = source.range()
            position = low + position * (high - low)
        self.scope.level = position
        dpg.set_value(self.items["level"], self.scope.level)

    def level_position(self, normalize: bool) -> float:
        source = self.scope.source
        if normalize and source is not None:
            low, high = source.range()
            return (self.scope.level - low) / (high - low)
        return self.scope.level

    def on_choice_level(self, sender, data):
        field = self.scope.source.field if self.scope.source else None
        if field is not None:
            self.scope.level = float(self.widget_value(field, "choice", data))
            dpg.set_value(self.items["level"], self.scope.level)

    def on_fit_window(self):
        self.scope.view = None

    def on_resize(self, *args):
        for bar, info in self.bars.items():
            info["width"] = 0
            dpg.configure_item(bar, width=-1)
        self.layout_dirty = True

    def on_normalize(self, sender, data):
        self.scope.captured = True
        for pane in self.panes():
            pane.y_limits = None
        for trace in self.scope.traces.values():
            trace.dirty = True

    def write_register(self, register: Register | None, value: int):
        if register is None:
            return
        value &= (1 << (register.width or 32)) - 1
        if self.comm is not None:
            self.background(register.write, value)
        register.value = value
        self.shown_value = None

    def refresh_sources(self):
        labels = [trace.label for trace in self.scope.traces.values()]
        dpg.configure_item(self.items["hint"], show=not labels)
        source = self.scope.source
        dpg.configure_item(self.items["source"], items=labels)
        dpg.set_value(self.items["source"], source.label if source else "")

        choices = choice_map(source.field) if source and source.field.encoding == Encoding.CHOICE else dict()
        dpg.configure_item(self.items["choice_level"], items=list(choices.values()), show=bool(choices))
        dpg.configure_item(self.items["level"], show=not choices)

    # per frame work

    def frame(self):
        self.drain()
        self.refresh_open()
        self.refresh_detail()
        self.refresh_bars()
        self.refresh_drag()
        self.refresh_left()
        self.refresh_favorites()
        self.refresh_plot()
        self.refresh_status()

    def refresh_drag(self):
        """Shows where a dragged field would land, the drop itself resolves the same zone again."""
        down = dpg.is_mouse_button_down(dpg.mvMouseButton_Left)
        pressed, self.mouse_down = down and not self.mouse_down, down

        if pressed and self.drag is None:
            self.dragged = next((field for item, field in self.row_fields.items()
                                 if dpg.does_item_exist(item) and dpg.get_item_state(item).get("active")), None)
        elif not down:
            self.dragged = None

        pane = None
        if self.dragged is not None:
            pane = next((p for p in self.panes() if dpg.get_item_state(p.plot).get("hovered")), None)

        if pane is None:
            dpg.configure_item(self.items["overlay"], show=False)
            return

        zone = self.drop_zone(pane.plot)
        pmin, pmax = self.zone_rect(pane.plot, zone)
        dpg.configure_item(self.items["zone"], pmin=pmin, pmax=pmax, fill=ZONE_ADD if zone == "center" else ZONE_SPLIT)
        dpg.configure_item(self.items["zone_text"], pos=(pmin[0] + 8, pmin[1] + 6),
                           text="add to this plot" if zone == "center" else f"new plot {zone}")
        dpg.configure_item(self.items["overlay"], show=True)

    def drain(self):
        if self.comm is None:
            return
        batch, self.comm.updates.updates = self.comm.updates.updates, dict()
        if not batch:
            return

        traces = dict()
        for trace in self.scope.traces.values():
            traces.setdefault(trace.register, []).append(trace)

        for register, updates in batch.items():
            self.updates += len(updates)
            for trace in traces.get(register, ()):
                for update in updates:
                    self.scope.feed(trace, update.time * 1e-9, numeric(trace.field, field_raw(trace.field, update.value)))

    def refresh_detail(self):
        register = self.selected
        if register is None or register.value == self.shown_value:
            return
        self.shown_value = register.value

        if not focused(self.items["raw"]):
            dpg.set_value(self.items["raw"], f"{register.value:08X}")

        for field, widget, kind in self.field_widgets:
            if not focused(widget):
                self.apply_widget(field, widget, kind)

    def refresh_open(self):
        """A tree node has no click callback, so opening one is picked up from its state."""
        for register, node in self.register_nodes.items():
            state = dpg.get_value(node)
            if state == self.open_nodes.get(node):
                continue
            self.open_nodes[node] = state
            if state and register is not self.selected:
                self.show_register(register)

    def refresh_bars(self):
        for bar in [b for b in self.bars if not dpg.does_item_exist(b)]:
            self.bars.pop(bar)

        down = dpg.is_mouse_button_down(dpg.mvMouseButton_Left)
        position = dpg.get_mouse_pos(local=False)[0]

        if self.drag is not None:
            bar = self.drag["bar"]
            if not dpg.does_item_exist(bar):
                self.drag = None
            elif down:
                if abs(position - self.drag["origin"]) > DRAG_THRESHOLD:
                    self.drag["moved"] = True
                if self.drag["moved"]:
                    self.drag_bar(bar, position)
            else:
                if not self.drag["moved"]:
                    self.open_entry(bar)
                self.drag = None
        elif down:
            for bar, info in self.bars.items():
                if info["writable"] and dpg.get_item_state(bar).get("hovered"):
                    self.drag = {"bar": bar, "origin": position, "moved": False}
                    break

        for bar, info in self.bars.items():
            if dpg.get_item_configuration(bar)["show"]:
                self.draw_bar(bar)

        if self.entry is not None:
            bar = self.entry["bar"]
            info = self.bars.get(bar)
            if info is None:
                self.entry = None
            else:
                # focus only lands on the entry a frame after it was shown
                self.entry["age"] += 1
                if self.entry["age"] > 2 and not focused(info["entry"]):
                    self.close_entry(bar)

    def drag_bar(self, bar: int, position: float):
        info = self.bars[bar]
        left = dpg.get_item_rect_min(bar)[0]
        width = max(1, dpg.get_item_rect_size(bar)[0])
        span = max(0.0, min(1.0, (position - left) / width))
        value = info["low"] + span * (info["high"] - info["low"])
        self.write_field(info["field"], info["edit"], value if info["edit"] == "float" else round(value))

    def open_entry(self, bar: int):
        if self.entry is not None:
            self.close_entry(self.entry["bar"])
        info = self.bars[bar]
        dpg.set_value(info["entry"], self.bar_value(info))
        dpg.configure_item(bar, show=False)
        dpg.configure_item(info["entry"], show=True)
        dpg.focus_item(info["entry"])
        self.entry = {"bar": bar, "age": 0}

    def close_entry(self, bar: int):
        info = self.bars.get(bar)
        if info is not None:
            dpg.configure_item(info["entry"], show=False)
            dpg.configure_item(bar, show=True)
        self.entry = None

    def refresh_left(self):
        """Only the expanded register and the visible search hits carry live values."""
        groups = dict(self.shown_rows)
        if self.selected is not None and not self.shown_rows:
            groups[self.selected] = self.tree_rows.get(self.selected, ())

        for register, rows in groups.items():
            if register.value == self.left_values.get(register):
                continue
            self.left_values[register] = register.value
            for field, item, label in rows:
                dpg.configure_item(item, label=f"{label}  {value_text(field)}")

    def refresh_favorites(self):
        for favorite in self.favorites:
            if favorite.register.value == favorite.shown:
                continue
            favorite.shown = favorite.register.value
            for node, widget, kind in favorite.widgets:
                if not focused(widget):
                    self.apply_widget(node, widget, kind)

    def apply_widget(self, node, widget, kind: str):
        if kind == "bar":
            return

        raw = node.value
        if kind == "raw":
            dpg.set_value(widget, f"0x{raw:X}")
        elif kind == "choice":
            dpg.set_value(widget, choice_map(node).get(raw, f"{raw}"))
        elif kind == "bool":
            dpg.set_value(widget, bool(raw))
        elif kind == "text":
            dpg.set_value(widget, decode(node, raw))
        else:
            dpg.set_value(widget, int(decode(node, raw)))

    def refresh_plot(self):
        scope = self.scope
        scope.update_period()
        if self.layout_dirty:
            self.relayout()
        normalize = dpg.get_value(self.items["normalize"])
        free_run = scope.mode == TRIGGER_MODES[0]

        if free_run and not self.plotted and not scope.ready():
            for trace in scope.traces.values():
                dpg.set_value(trace.series, [[], []])
            return

        if scope.take_capture():
            previous = scope.reference
            scope.update_reference()
            rebase = scope.reference != previous

            for trace in scope.traces.values():
                if trace.dirty or rebase or not self.plotted:
                    if normalize:
                        low, high = trace.range()
                        values = [(value - low) / (high - low) for value in trace.value]
                    else:
                        values = list(trace.value)
                    dpg.set_value(trace.series, [[t - scope.reference for t in trace.time], values])
                    trace.dirty = False
            self.plotted = True

        self.update_view()
        source = scope.source
        for pane in self.panes():
            self.fit_y(pane, normalize)
            on_source = source is not None and source.pane is pane
            dpg.configure_item(pane.trigger, show=on_source and not free_run)
            if on_source:
                dpg.set_value(pane.trigger, self.level_position(normalize))

    def update_view(self):
        """The x axis belongs to the user, the timebase follows whatever is panned into view."""
        scope = self.scope
        panes = self.panes()
        # applying limits before the plot has drawn once gets overridden by its own initial fit
        if self.plotted and panes:
            master = next((p for p in panes if dpg.get_item_state(p.plot).get("hovered")), panes[0])
            for pane in panes:
                if pane is not master:
                    dpg.set_axis_limits(pane.x_axis, *scope.limits())
                    pane.locked = True
                elif scope.view is None:
                    scope.view = scope.default_view()
                    dpg.set_axis_limits(pane.x_axis, *scope.view)
                    pane.locked = True
                elif pane.locked:
                    dpg.set_axis_limits_auto(pane.x_axis)
                    pane.locked = False
                else:
                    scope.view = tuple(dpg.get_axis_limits(pane.x_axis))

        pre, post = scope.samples()
        low, high = scope.limits()
        dpg.set_value(self.items["window"], f"{high - low:.3f} s  -{pre} / +{post}")

    def fit_y(self, pane: Pane, normalize: bool):
        """Refitting every frame makes the plot shimmer, so only follow real range changes."""
        if not dpg.get_value(self.items["autofit"]):
            if pane.y_limits is not None:
                dpg.set_axis_limits_auto(pane.y_axis)
                pane.y_limits = None
            return

        now = time.perf_counter()
        if pane.y_limits is not None and now - pane.fit_time < 0.5:
            return
        pane.fit_time = now

        low = high = None
        for trace in pane.traces:
            if not trace.value:
                continue
            trace_low, trace_high = min(trace.value), max(trace.value)
            if normalize:
                range_low, range_high = trace.range()
                trace_low = (trace_low - range_low) / (range_high - range_low)
                trace_high = (trace_high - range_low) / (range_high - range_low)
            low = trace_low if low is None else min(low, trace_low)
            high = trace_high if high is None else max(high, trace_high)

        if low is None:
            return
        margin = 0.05 * (high - low) or 0.5
        low, high = low - margin, high + margin

        if pane.y_limits is not None:
            span = pane.y_limits[1] - pane.y_limits[0]
            if abs(low - pane.y_limits[0]) < 0.05 * span and abs(high - pane.y_limits[1]) < 0.05 * span:
                return

        pane.y_limits = (low, high)
        dpg.set_axis_limits(pane.y_axis, low, high)

    def refresh_status(self):
        now = time.perf_counter()
        elapsed = now - self.rate_time
        if elapsed >= 0.5:
            self.update_rate = self.updates / elapsed
            self.updates = 0
            self.rate_time = now

        connection = f"{self.options.comm_type}" if self.comm is not None else "offline"
        pending = self.comm.requests.open_requests() if self.comm is not None else 0
        dpg.set_value(
            self.items["status"],
            f"{connection}   {self.update_rate:7.1f} samples/s   {pending:4d} pending   "
            f"scope {self.scope.state}   {len(self.scope.traces)} traces   {dpg.get_frame_rate():.0f} fps",
        )
