"""Synthetic Python fixture: class constants, constructor writes, later writes."""

CONFIG_PATH = "/etc/iap/config.yaml"


class Service:
    kind = "worker"
    retries = 3

    def __init__(self):
        self.status = "new"
        self.limit = 10

    def close(self):
        self.status = "processed"


class Counter:
    def __init__(self):
        self.count = 0

    def bump(self):
        self.count = self.count + 1


class WithProperty:
    def __init__(self):
        self._value = 5

    @property
    def value(self):
        return self._value
