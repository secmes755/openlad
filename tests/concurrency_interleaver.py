"""Deterministic thread interleaving for concurrency tests.

A shared object's per-call state only goes wrong when two threads overlap
in a window that a single-threaded test cannot reach: thread A writes a
slot, thread B overwrites it, A reads back B's value. Reproducing that
with ``time.sleep`` is slow and flaky — the sleep is either too short to
force the overlap or long enough that the test drags.

``Interleaver`` replaces sleeping with explicit rendezvous. A thread calls
``at("name")`` to announce it has reached a point and then blocks until
another thread calls ``release("name")``. Because the wait is a
``threading.Event``, ordering is exact and the test finishes as fast as
the code under test allows.
"""
import threading


class Interleaver:
    """Coordinate two threads at named points, with no sleeping.

    Usage::

        il = Interleaver()

        def thread_a():
            thing.write(1)
            il.at("a-wrote")          # announce, then wait
            return thing.read()       # runs only after release

        def thread_b():
            il.wait_for("a-wrote")    # block until A is parked
            thing.write(2)            # clobber A's value
            il.release("a-wrote")     # let A continue

    ``at()`` both signals and waits; ``wait_for()`` waits without
    signalling; ``release()`` unblocks a waiter of ``at()``.
    """

    def __init__(self, timeout: float = 5.0):
        self._timeout = timeout
        self._arrived = {}
        self._go = {}
        self._lock = threading.Lock()

    def _events(self, name):
        with self._lock:
            if name not in self._arrived:
                self._arrived[name] = threading.Event()
                self._go[name] = threading.Event()
            return self._arrived[name], self._go[name]

    def at(self, name: str) -> None:
        """Announce arrival at ``name``, then block until released.

        Returns True if released, False on timeout (so a test can fail
        with a message instead of hanging forever).
        """
        arrived, go = self._events(name)
        arrived.set()
        if not go.wait(self._timeout):
            raise TimeoutError(
                f"Interleaver: nobody released point {name!r} within "
                f"{self._timeout}s — the other thread likely never arrived"
            )

    def wait_for(self, name: str) -> bool:
        """Block until another thread calls ``at(name)``."""
        arrived, _ = self._events(name)
        if not arrived.wait(self._timeout):
            raise TimeoutError(
                f"Interleaver: point {name!r} was never reached within "
                f"{self._timeout}s"
            )
        return True

    def release(self, name: str) -> None:
        """Let a thread parked in ``at(name)`` continue."""
        _, go = self._events(name)
        go.set()
