"""Portable, bounded worker execution for local M2 parsing stages."""
from __future__ import annotations

import multiprocessing
from pathlib import Path
import pickle
import tempfile
import time


def _worker(function, arguments, destination):
    try:
        result = (True, function(*arguments))
    except Exception as exc:
        result = (False, exc)
    with open(destination, "wb") as stream:
        pickle.dump(result, stream, protocol=pickle.HIGHEST_PROTOCOL)


def call_with_timeout(function, arguments, seconds):
    """The child only reads source; the parent alone publishes checkpoints.

    The pickle is produced by our own child in a private temporary directory,
    never loaded from a corpus or a persisted/untrusted checkpoint.
    """
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="m2-worker-") as temporary:
        destination = Path(temporary) / "result.pickle"
        process = context.Process(target=_worker, args=(function, arguments, destination))
        process.start()
        try:
            process.join(seconds)
            if process.is_alive():
                raise TimeoutError("M2 version exceeded its parsing deadline")
            if process.exitcode != 0 or not destination.is_file():
                raise RuntimeError("M2 worker exited without a valid result")
            with destination.open("rb") as stream:
                valid, value = pickle.load(stream)
            if not valid:
                raise value
            return value
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join()
            process.close()


class VersionExecutor:
    """Share one finite time budget across preparation/rendering of each version."""
    def __init__(self, seconds):
        self.seconds = seconds
        self.spent = {}

    def __call__(self, version, function, arguments):
        remaining = self.seconds - self.spent.get(version, 0.0)
        if remaining <= 0:
            raise TimeoutError("M2 version exceeded its parsing deadline")
        started = time.monotonic()
        try:
            return call_with_timeout(function, arguments, remaining)
        finally:
            self.spent[version] = self.spent.get(version, 0.0) + time.monotonic() - started
