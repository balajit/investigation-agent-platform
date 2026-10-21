"""Synthetic Python fixture: branches, returns, raises, async/generator."""


def classify(score):
    if score > 90:
        return "excellent"
    if score > 50:
        return "pass"
    raise ValueError("too low")


async def stream():
    yield 1
    yield 2
