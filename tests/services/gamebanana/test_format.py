import pytest

from src.services.gamebanana.format import humanize_size


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (1024 * 1024 * 1024, "1.0 GB"),
        (int(1024 * 1024 * 1024 * 2.5), "2.5 GB"),
        (1024**4, "1.0 TB"),
    ],
)
def test_humanize_size(n, expected):
    assert humanize_size(n) == expected


def test_humanize_size_negative_clamps_to_zero():
    assert humanize_size(-100) == "0 B"
