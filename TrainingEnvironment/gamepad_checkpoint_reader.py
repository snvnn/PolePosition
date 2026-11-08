"""Pickle interface for torch.load while the online worker owns a native heartbeat.

CPython's Python implementation of Unpickler lets the heartbeat threads run
between pickle operations on large replay lists. The default C implementation
can hold the GIL for the entire unpickle. Use the existing trusted checkpoint
format so previous models, optimizer state, replay and RNGs load identically.
"""
from pickle import _Unpickler as Unpickler


def load(stream, **kwargs):
    return Unpickler(stream, **kwargs).load()
