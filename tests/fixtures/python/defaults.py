"""Synthetic Python fixture: module defaults and parameter defaults."""

timeout = 30

retries = 3

ratio = 2.5

debug = False

name = "worker"

nothing = None

# Annotation only: no binding, must stay unresolved.
pending: int

# Chained assignment: both names resolve to the same literal.
low = high = 5

# Unpacked assignment.
first, second = 1, 2


def fetch(limit=10, verbose=False):
    return limit


def append_item(items=[]):
    return items


def load():
    return 1


# Call-site value must never back-propagate into the declared default.
result = fetch(20)
