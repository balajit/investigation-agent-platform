"""Synthetic Python fixture: in-window inheritance and overrides."""

from ext import ExternalBase


class Base:
    def handle(self):
        return "base"

    def ping(self):
        return "base-ping"


class Mixin:
    def ping(self):
        return "mixin-ping"


class Service(Base):
    def handle(self):
        return "service"


class Combo(Service, Mixin):
    pass


class Extended(ExternalBase):
    pass
