import pytest


def test_speed_uses_received_bytes_over_elapsed_time():
    from src.services.app_update.progress import DownloadRate

    clock = [0.0]
    rate = DownloadRate(clock=lambda: clock[0])
    rate.record(1024 * 1024)
    clock[0] = 0.5
    assert rate.sample() == pytest.approx(2 * 1024 * 1024)
    rate.record(2 * 1024 * 1024)
    clock[0] = 1.0
    assert rate.sample() == pytest.approx(2 * 1024 * 1024)


def test_a_stalled_transfer_displays_zero_instead_of_the_previous_speed():
    from src.services.app_update.progress import DownloadRate

    clock = [0.0]
    rate = DownloadRate(clock=lambda: clock[0])
    rate.record(1024)
    clock[0] = 0.5
    assert rate.sample() == 2048
    clock[0] = 1.0
    assert rate.sample() == 0


def test_restarting_the_byte_counter_resets_speed_sampling():
    from src.services.app_update.progress import DownloadRate

    clock = [0.0]
    rate = DownloadRate(clock=lambda: clock[0])
    rate.record(1024 * 1024)
    clock[0] = 0.5
    rate.sample()
    rate.record(128 * 1024)
    assert rate.sample() is None
    clock[0] = 1.0
    assert rate.sample() == 256 * 1024
    rate.reset()
    assert rate.sample() is None


def test_sampling_immediately_does_not_divide_by_zero():
    from src.services.app_update.progress import DownloadRate

    rate = DownloadRate(clock=lambda: 0.0)
    rate.record(42)
    assert rate.sample() is None


def test_resumed_bytes_are_not_counted_as_new_download_speed():
    from src.services.app_update.progress import DownloadRate

    clock = [0.0]
    rate = DownloadRate(clock=lambda: clock[0])
    rate.reset(100 * 1024 * 1024)
    rate.record(100 * 1024 * 1024 + 1024)
    clock[0] = 0.5
    assert rate.sample() == 2048
