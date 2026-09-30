import httpx
import pytest

from src.services.app_update import checker as checker_module
from src.services.app_update.checker import AppUpdateChecker


def release(version, *, body="Release notes", **extra):
    return {
        "tag_name": f"v{version}",
        "name": f"Version {version}",
        "body": body,
        "published_at": "2026-10-27T02:00:00Z",
        "html_url": f"https://github.com/example/app/releases/tag/v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [{"name": "app.zip", "size": 1024, "browser_download_url": f"https://example.com/{version}/app.zip"}],
        **extra,
    }


@pytest.fixture
def serve(monkeypatch, temp_data_dir):
    """仅替换 HTTP 传输，保留真实请求校验和检查器。"""
    client = httpx.Client
    monkeypatch.setattr(checker_module, "APP_UPDATE_API_MIRRORS", ("",))

    def configure(latest, history, *, history_status=200):
        requests = []

        def handle(request):
            requests.append(request.url)
            if request.url.path.endswith("/releases/latest"):
                return httpx.Response(200, json=latest)
            if request.url.path.endswith("/releases"):
                return httpx.Response(history_status, json=history)
            raise AssertionError(f"Unexpected request: {request.url}")

        transport = httpx.MockTransport(handle)
        monkeypatch.setattr(checker_module.httpx, "Client", lambda **kw: client(transport=transport, **kw))
        return requests

    return configure


def test_history_is_ordered_deduplicated_and_excludes_unpublished_releases(serve):
    latest = release("2.0.0", body="Authoritative latest notes")
    requests = serve(
        latest,
        [
            release("1.0.0"),
            release("3.0.0", prerelease=True),
            release("2.0.0", body="Stale mirror notes"),
            release("1.8.0"),
            release("1.9.0", draft=True),
            release("1.8.0"),
        ],
    )
    info = AppUpdateChecker("1.0.0", "example/app").check()
    notes = info.to_dict()["releases"]
    assert [item["version"] for item in notes] == ["2.0.0", "1.8.0", "1.0.0"]
    assert notes[0]["changelog"] == "Authoritative latest notes"
    assert notes[1]["published_at"] == "2026-10-27T02:00:00Z"
    assert any(url.params.get("per_page") == "30" for url in requests)


def test_history_never_replaces_latest_download_metadata(serve):
    latest = release("2.0.0", body="SHA256: " + "a" * 64)
    serve(latest, [release("1.0.0"), release("2.1.0")])
    info = AppUpdateChecker("1.0.0", "example/app").check()
    assert info.has_update
    assert len(info.to_dict()["releases"]) == 3
    assert info.latest_version == "2.0.0"
    assert info.download_url == "https://example.com/2.0.0/app.zip"
    assert info.download_size == 1024
    assert info.sha256 == "a" * 64


@pytest.mark.parametrize("status,history", [(503, {}), (200, {"message": "not a list"})])
def test_history_failure_does_not_block_a_valid_update(serve, status, history):
    serve(release("2.0.0"), history, history_status=status)
    info = AppUpdateChecker("1.0.0", "example/app").check()
    assert info.error is None
    assert info.has_update
    assert info.download_url == "https://example.com/2.0.0/app.zip"
    result = info.to_dict()
    assert [item["version"] for item in result["releases"]] == ["2.0.0"]
    assert result["history_error"]


def test_current_version_still_has_browsable_release_notes(serve):
    serve(release("2.0.0"), [release("2.0.0"), release("1.0.0")])
    info = AppUpdateChecker("2.0.0", "example/app").check()
    assert not info.has_update
    assert [item["version"] for item in info.to_dict()["releases"]] == ["2.0.0", "1.0.0"]
    assert not info.download_url


def test_empty_history_keeps_latest_notes_without_warning(serve):
    serve(release("2.0.0"), [])
    result = AppUpdateChecker("1.0.0", "example/app").check().to_dict()
    assert [item["version"] for item in result["releases"]] == ["2.0.0"]
    assert not result["history_error"]


def test_malformed_history_entries_do_not_hide_valid_notes(serve):
    serve(release("2.0.0"), [None, {}, {"tag_name": None}, release("1.0.0")])
    result = AppUpdateChecker("1.0.0", "example/app").check().to_dict()
    assert [item["version"] for item in result["releases"]] == ["2.0.0", "1.0.0"]
