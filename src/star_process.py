"""Own a headless provider until Star releases its control pipe.

This process, rather than Star, is the provider's parent. It keeps the provider
unreaped while signalling its process group, so an old PID can never target an
unrelated process. A second conversation lock covers crash cleanup. The two
phase start means a guardian left behind before Star confirms readiness cannot
launch a provider after another Star has resumed the conversation.
"""

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time

from star_sessions import Lease


def pump(source, target, exited):
    """Copy provider output to this guardian's own stream. A tool the provider
    left running can hold the pipe open indefinitely, so once the provider has
    exited, stop when the pipe goes quiet (all it wrote is already buffered)."""
    deadline = None
    while True:
        if exited.is_set() and deadline is None:
            deadline = time.monotonic() + 2
        if deadline and time.monotonic() > deadline:
            return
        if not select.select([source], [], [], .2)[0]:
            if exited.is_set():
                return
            continue
        try:
            data = os.read(source, 65536)
        except OSError:
            return
        if not data:
            return
        view = memoryview(data)
        while view:
            view = view[os.write(target, view):]


def own(argv):
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as exc:
        raise OSError(f"{argv[0]}: {exc.strerror or exc}") from exc
    exited = threading.Event()
    pumps = [threading.Thread(target=pump, args=(stream.fileno(), fd, exited), daemon=True)
                            for stream, fd in ((proc.stdout, 1), (proc.stderr, 2))]
    for thread in pumps:
        thread.start()
    gate = threading.Lock()
    finished = False

    def stop():
        nonlocal finished
        with gate:
            if finished:
                return
            # The child has not been reaped while this lock is held. Its PID
            # and process group cannot be reused during the escalation.
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                if sig != signal.SIGKILL:
                    time.sleep(1)
            proc.wait()
            finished = True

    def watch():
        # A command or EOF both stop this turn. Native provider stdin stays
        # disconnected, retaining each engine's headless behavior.
        # Unbuffered OS reads avoid Python finalization waiting for a buffered
        # stdin lock held by this daemon after a provider completes normally.
        os.read(sys.stdin.fileno(), 1)
        stop()

    threading.Thread(target=watch, daemon=True).start()
    try:
        while True:
            with gate:
                code = proc.poll()
                if code is not None:
                    finished = True
                    return code
            time.sleep(.03)
    finally:
        stop()
        exited.set()
        for thread in pumps:
            thread.join()


def main():
    config = json.loads(sys.stdin.buffer.readline())
    folder, ident = config.get("locks"), config.get("conversation")
    with Lease(folder, check_turn=False) if folder else _NoLease() as lease:
        if folder:
            lease.claim(ident)
        print('{"star_guard":"ready"}', flush=True)
        if sys.stdin.buffer.readline().strip() != b"go":
            return 130
        return own(config["argv"])


class _NoLease:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Star provider process: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
