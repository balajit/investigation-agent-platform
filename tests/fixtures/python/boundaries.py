"""Synthetic Python fixture: unsupported/dynamic constructs that must abstain."""

import os

timeout = int(os.getenv("TIMEOUT", "30"))

computed = load_timeout()

value = items[0]

owner = service.name

text = f"timeout={timeout}"

squares = [x * x for x in range(10)]

mode = "fast" if debug else "slow"
