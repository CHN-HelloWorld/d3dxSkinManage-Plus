from threading import Event, Lock, Thread
from threading import enumerate as threads

import pytest


@pytest.mark.parametrize("failure", ["cancel", "permission"])
def test_identity_batch_limits_submission_and_joins_readers_on_exit(failure):
    from src.core.library_snapshot import bounded_identity_reads

    cancelled, release, four_readers, error_raised, done = (Event() for _ in range(5))
    lock = Lock()
    enumerated, results, errors = [], [], []
    active = peak = 0

    def entries():
        for item in range(100):
            enumerated.append(item)
            yield item

    def read(item):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
            if active == 4:
                four_readers.set()
        try:
            if item == 0 and failure == "permission":
                assert four_readers.wait(5)
                error_raised.set()
                raise PermissionError("unreadable marker")
            assert release.wait(5)
            return item
        finally:
            with lock:
                active -= 1

    def consume():
        try:
            results.extend(bounded_identity_reads(entries(), read, cancelled.is_set))
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    runner = Thread(target=consume)
    runner.start()
    try:
        assert four_readers.wait(2)
        assert enumerated == list(range(32))
        if failure == "cancel":
            cancelled.set()
        else:
            assert error_raised.wait(2)
        assert not done.wait(0.05), "The probe must join active readers before returning"
        release.set()
        assert done.wait(2)
        assert len(errors) == 1
        assert isinstance(errors[0], InterruptedError if failure == "cancel" else PermissionError)
        assert peak == 4 and active == 0
        assert not any(thread.name.startswith("source-identity") for thread in threads())
    finally:
        cancelled.set()
        release.set()
        runner.join(5)
