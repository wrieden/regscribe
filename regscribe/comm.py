

class RegisterMonitor:
    def __init__(self):
        self.monitored = dict()
        self.it = iter(self.monitored.values())


    def add_listener(self, node, name, prio):
        if prio == 0:
            self.remove_listener(node, name)
            return

        mon = self.monitored.pop(node, Monitored(node))
        mon.add_listener(name, prio)
        self.monitored[node] = mon

        self.it = iter(self.monitored.values())


    def remove_listener(self, node, name):
        mon = self.monitored.pop(node, Monitored(node))
        mon.remove_listener(name)
        if mon.has_listener():
            self.monitored[node] = mon

        self.it = iter(self.monitored.values())


    def get_next(self):
        while True:
            mon = next(self.it, None)
            if mon is None:
                self.it = iter(self.monitored.values())
                mon = next(self.it, None)
            if mon is None:
                return None

            if mon.counter <= 1:
                mon.reset_counter()
                return mon.node
            else:
                mon.counter -= 1

class Monitored:
    def __init__(self, node):
        self.node = node
        self.priority = dict()
        self.counter = 1

    def add_listener(self, name, priority):
        self.priority[name] = priority
        self.counter = min([self.counter, self.lowest_priority()])

    def reset_counter(self):
        self.counter = self.lowest_priority()

    def remove_listener(self, name):
        self.priority.pop(name, None)

    def lowest_priority(self):
        return min(self.priority.values())

    def has_listener(self):
        return bool(self.priority)

