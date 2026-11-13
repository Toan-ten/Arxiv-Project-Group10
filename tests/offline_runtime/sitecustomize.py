"""Opt-in offline validation bootstrap, inherited by Python subprocesses.

Put this directory and the repository root on PYTHONPATH when running pytest.
Loopback servers remain available for the existing interruption tests. Default
runner scratch is redirected under each test's local root; production defaults
are unchanged because this directory is not on the runtime import path.
"""
import ipaddress
import socket
from pathlib import Path

from src.fast_production import FastProductionRunner

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo
_original_init = FastProductionRunner.__init__


def _require_loopback(host):
    if host == "localhost":
        return
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise AssertionError("External network forbidden during offline validation")


def _connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _require_loopback(address[0])
    return _original_connect(sock, address)


def _connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _require_loopback(address[0])
    return _original_connect_ex(sock, address)


def _getaddrinfo(host, *args, **kwargs):
    _require_loopback(host)
    return _original_getaddrinfo(host, *args, **kwargs)


def _isolated_init(self, *args, **kwargs):
    if kwargs.get("scratch_root") is None:
        local = kwargs.get("local_root", args[0] if args else None)
        kwargs["scratch_root"] = Path(local) / "scratch"
    return _original_init(self, *args, **kwargs)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.getaddrinfo = _getaddrinfo
FastProductionRunner.__init__ = _isolated_init
