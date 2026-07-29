"""Pure processing functions: boundary-flap debounce and segment matching.

These take plain ``ProcEvent`` lists and return plain results -- no DB, no
config objects, no I/O -- so they are trivially unit-testable (see
tests/test_debounce.py and tests/test_segments.py).
"""
