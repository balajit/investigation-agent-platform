"""Synthetic Python fixture: rebinding, deletion, and context-bound names."""

base = 1
base += 2

for looped in range(3):
    pass

target = 0
for target in range(3):
    pass

gone = 5
del gone

maybe = 7
if True:
    del maybe

with open("f") as handle:
    pass
