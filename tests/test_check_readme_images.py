"""The release-time guard that every image in the PyPI long description resolves.

Two halves are tested here. The rules -- what counts as resolving, what is worth retrying, what a
relative path means -- are exercised with an injected fetcher, so the whole suite still runs with
no network. The wiring is pinned separately: this guard lives in YAML as well as Python, and a job
that is not in `publish-pypi`'s `needs:` is not a gate at all, however correct its code is.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import zipfile
from email.message import Message
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

_SCRIPT = REPO / "scripts" / "check_readme_images.py"
_spec = importlib.util.spec_from_file_location("check_readme_images_under_test", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
# Registered before execution, not after: `@dataclass` resolves its own module out of `sys.modules`
# while the class body runs, and a module missing from there fails to import with an AttributeError
# that says nothing about the real cause.
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)


def _metadata(long_description: str) -> bytes:
    message = Message()
    message["Metadata-Version"] = "2.4"
    message["Name"] = "gdmutant"
    message["Version"] = "0.1.0"
    message["Description-Content-Type"] = "text/markdown"
    message.set_payload(long_description)
    return message.as_string().encode("utf-8")


def _wheel(directory: Path, long_description: str) -> Path:
    path = directory / "gdmutant-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("gdmutant/__init__.py", "")
        archive.writestr("gdmutant-0.1.0.dist-info/METADATA", _metadata(long_description))
    return path


def _sdist(directory: Path, long_description: str) -> Path:
    path = directory / "gdmutant-0.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        payload = _metadata(long_description)
        info = tarfile.TarInfo("gdmutant-0.1.0/PKG-INFO")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return path


def _fetcher(answers: dict[str, check.Response], calls: list[str] | None = None) -> check.Fetcher:
    def fetch(url: str) -> check.Response:
        if calls is not None:
            calls.append(url)
        return answers[url]

    return fetch


def _ok(content_type: str = "image/svg+xml", final_url: str = "") -> check.Response:
    return check.Response(status=200, content_type=content_type, final_url=final_url)


# --- Reading the built distribution, not the file on disk ---------------------------------------


def test_reads_the_long_description_out_of_a_wheel(tmp_path: Path) -> None:
    _wheel(tmp_path, '<img src="https://example.test/a.svg">')
    text, source = check.long_description(tmp_path)
    assert "https://example.test/a.svg" in text
    assert source.endswith(".whl")


def test_falls_back_to_the_sdist_when_there_is_no_wheel(tmp_path: Path) -> None:
    _sdist(tmp_path, '<img src="https://example.test/a.svg">')
    text, source = check.long_description(tmp_path)
    assert "https://example.test/a.svg" in text
    assert source.endswith(".tar.gz")


def test_an_empty_dist_dir_is_an_error_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        check.long_description(tmp_path)


def test_the_built_description_is_what_is_read_not_readme_md(tmp_path: Path) -> None:
    """The point of the whole script: those two texts differ, deliberately.

    hatch-fancy-pypi-readme rewrites the banner's relative src at build time, so a check that read
    README.md would be checking a string PyPI never receives -- and would pass while the real long
    description shipped something broken. Proven by giving the built artifact an image the source
    file does not contain.
    """
    _wheel(tmp_path, '<img src="https://raw.githubusercontent.test/kphutt/gdmutant/v0.1.0/b.svg">')
    text, _ = check.long_description(tmp_path)
    assert "v0.1.0" in text
    assert 'src=".github/assets/' in (REPO / "README.md").read_text(encoding="utf-8")
    assert 'src=".github/assets/' not in text


# --- Finding the images -------------------------------------------------------------------------


def test_finds_markdown_html_and_srcset_images() -> None:
    text = (
        "![a](https://example.test/md.png)\n"
        '<img alt="b" src="https://example.test/html.svg">\n'
        '<picture><source srcset="https://example.test/one.png 1x, '
        'https://example.test/two.png 2x">'
        "</picture>\n"
    )
    assert check.image_urls(text) == [
        "https://example.test/md.png",
        "https://example.test/html.svg",
        "https://example.test/one.png",
        "https://example.test/two.png",
    ]


def test_a_markdown_title_is_not_swallowed_into_the_url() -> None:
    assert check.image_urls('![a](https://example.test/x.png "a title")') == [
        "https://example.test/x.png"
    ]


def test_a_link_is_not_an_image() -> None:
    """`<a href>` and a plain markdown link render as text, not as an image that can 404."""
    text = '<a href="https://example.test/page"><img src="https://example.test/badge.svg"></a>'
    assert check.image_urls(text) == ["https://example.test/badge.svg"]


def test_the_same_image_twice_is_asked_about_once() -> None:
    text = '<img src="https://example.test/x.svg"><img src="https://example.test/x.svg">'
    assert check.image_urls(text) == ["https://example.test/x.svg"]


def test_the_real_readme_has_images_to_find() -> None:
    """Grounds every rule above against the actual file, so a README rewrite cannot leave this
    suite green while the extractor silently finds nothing."""
    urls = check.image_urls((REPO / "README.md").read_text(encoding="utf-8"))
    assert len(urls) >= 2, urls


# --- What counts as resolving --------------------------------------------------------------------


def test_a_relative_path_is_broken_without_asking_anyone() -> None:
    """THE DEFECT THIS SCRIPT EXISTS FOR. It needs no network to be certain."""
    verdict = check.check_url(".github/assets/banner.svg", _fetcher({}))
    assert verdict.state == "broken"
    assert "does not resolve a path" in verdict.detail


def test_a_data_uri_is_broken_because_pypi_strips_it() -> None:
    verdict = check.check_url("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=", _fetcher({}))
    assert verdict.state == "broken"
    assert "readme_renderer" in verdict.detail


def test_a_200_resolves() -> None:
    url = "https://example.test/x.svg"
    assert check.check_url(url, _fetcher({url: _ok()})).state == "ok"


def test_a_404_is_broken() -> None:
    url = "https://example.test/missing.svg"
    verdict = check.check_url(url, _fetcher({url: check.Response(status=404)}))
    assert verdict.state == "broken"
    assert "404" in verdict.detail


def test_a_github_404_explains_the_three_usual_causes() -> None:
    """The message a maintainer will most likely meet: the tag, the file, or a private repo.

    While the repository is private, `raw.githubusercontent.com` 404s an anonymous request -- and
    anonymous is exactly what PyPI's camo proxy is, so the banner really would be broken. The guard
    is right to fail; the log has to say why, or it reads as a bug in the guard.
    """
    url = "https://raw.githubusercontent.com/kphutt/gdmutant/v0.1.0/.github/assets/banner.svg"
    verdict = check.check_url(url, _fetcher({url: check.Response(status=404)}))
    assert verdict.state == "broken"
    assert "still private" in verdict.detail and "tag is not pushed" in verdict.detail


def test_a_404_somewhere_else_does_not_get_the_github_hint() -> None:
    url = "https://img.shields.io/pypi/v/gdmutant"
    verdict = check.check_url(url, _fetcher({url: check.Response(status=404)}))
    assert verdict.state == "broken"
    assert "private" not in verdict.detail


def test_a_200_that_serves_html_is_broken() -> None:
    """A `.../blob/...` GitHub URL answers 200 with a web page; camo proxies the page and the
    reader sees a broken image. Only the `raw` form serves the bytes."""
    url = "https://github.com/kphutt/gdmutant/blob/main/.github/assets/banner.svg"
    verdict = check.check_url(url, _fetcher({url: _ok(content_type="text/html; charset=utf-8")}))
    assert verdict.state == "broken"
    assert "HTML page" in verdict.detail


def test_plain_text_is_not_treated_as_html() -> None:
    """raw.githubusercontent serves some files as text/plain; only text/html is the page trap."""
    url = "https://raw.githubusercontent.test/x.svg"
    assert check.check_url(url, _fetcher({url: _ok(content_type="text/plain")})).state == "ok"


def test_a_followed_redirect_resolves_and_says_where_it_went() -> None:
    url = "https://example.test/redirected.svg"
    response = _ok(final_url="https://cdn.example.test/redirected.svg")
    verdict = check.check_url(url, _fetcher({url: response}))
    assert verdict.state == "ok"
    assert "cdn.example.test" in verdict.detail


def test_an_unfollowed_redirect_is_reported_rather_than_assumed_fine() -> None:
    url = "https://example.test/moved.svg"
    verdict = check.check_url(url, _fetcher({url: check.Response(status=301)}))
    assert verdict.state == "broken"


def test_a_missing_status_is_unverified_not_broken() -> None:
    url = "https://example.test/x.svg"
    verdict = check.check_url(url, _fetcher({url: check.Response()}), attempts=1)
    assert verdict.state == "unverified"


# --- Flaky networks must not fail a release spuriously -------------------------------------------


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_a_busy_host_is_retried_and_reported_as_unknown_not_broken(status: int) -> None:
    """A gate that fails randomly gets switched off, which is worse than no gate. A server that is
    up but unhappy is a different fact from an image that is not there, and gets a different exit
    code so the log says which."""
    url = "https://example.test/x.svg"
    calls: list[str] = []
    verdict = check.check_url(
        url, _fetcher({url: check.Response(status=status)}, calls), attempts=3, sleep=lambda _: None
    )
    assert verdict.state == "unverified"
    assert len(calls) == 3, "a transient answer must be retried"


def test_a_transport_failure_is_retried_then_reported_as_unknown() -> None:
    url = "https://example.test/x.svg"
    calls: list[str] = []
    fetch = _fetcher({url: check.Response(error="TimeoutError: timed out")}, calls)
    verdict = check.check_url(url, fetch, attempts=3, sleep=lambda _: None)
    assert verdict.state == "unverified"
    assert "timed out" in verdict.detail
    assert len(calls) == 3


def test_a_retry_that_succeeds_resolves() -> None:
    url = "https://example.test/x.svg"
    answers = [check.Response(status=503), _ok()]
    verdict = check.check_url(url, lambda _: answers.pop(0), attempts=3, sleep=lambda _: None)
    assert verdict.state == "ok"
    assert not answers, "it must stop asking once it has an answer"


def test_a_404_is_not_retried() -> None:
    """Retrying a fact only delays the report by a minute."""
    url = "https://example.test/missing.svg"
    calls: list[str] = []
    check.check_url(url, _fetcher({url: check.Response(status=404)}, calls), attempts=5)
    assert len(calls) == 1


def test_the_backoff_lengthens_between_attempts() -> None:
    url = "https://example.test/x.svg"
    waits: list[float] = []
    check.check_url(
        url,
        _fetcher({url: check.Response(status=503)}),
        attempts=3,
        delay=2.0,
        sleep=waits.append,
    )
    assert waits == [2.0, 4.0]


# --- The command-line behaviour -------------------------------------------------------------------


def _run(tmp_path: Path, long_description: str, answers: dict[str, check.Response]) -> int:
    _wheel(tmp_path, long_description)
    return check.main(
        ["check_readme_images.py", "--dist-dir", str(tmp_path), "--attempts", "1"],
        fetch=_fetcher(answers),
        sleep=lambda _: None,
    )


def test_main_passes_when_every_image_resolves(tmp_path: Path) -> None:
    url = "https://example.test/x.svg"
    assert _run(tmp_path, f'<img src="{url}">', {url: _ok()}) == check.OK


def test_main_fails_on_a_broken_image(tmp_path: Path) -> None:
    url = "https://example.test/x.svg"
    assert _run(tmp_path, f'<img src="{url}">', {url: check.Response(status=404)}) == check.BROKEN


def test_main_separates_a_flaky_network_from_a_broken_image(tmp_path: Path) -> None:
    """Different exit codes because they need different actions: one is a new version number, the
    other is a re-run."""
    url = "https://example.test/x.svg"
    code = _run(tmp_path, f'<img src="{url}">', {url: check.Response(error="timeout")})
    assert code == check.UNVERIFIED


def test_a_broken_image_outranks_an_unverified_one(tmp_path: Path) -> None:
    """A known-broken image must not be downgraded to "could not check" by an unrelated flake."""
    broken, flaky = "https://example.test/gone.svg", "https://example.test/slow.svg"
    code = _run(
        tmp_path,
        f'<img src="{broken}"><img src="{flaky}">',
        {broken: check.Response(status=404), flaky: check.Response(error="timeout")},
    )
    assert code == check.BROKEN


def test_finding_no_images_fails_rather_than_reporting_success_on_nothing(tmp_path: Path) -> None:
    """The silent pass this guard exists to prevent. The README has images; finding none means the
    extraction broke, and a green tick would then certify an unchecked long description."""
    assert _run(tmp_path, "no images here at all", {}) == check.BROKEN


def test_main_reports_usage_when_there_is_no_distribution(tmp_path: Path) -> None:
    assert check.main(["check_readme_images.py", "--dist-dir", str(tmp_path)]) == check.USAGE


def test_the_report_names_every_url_not_just_the_first_bad_one(tmp_path: Path) -> None:
    verdicts = [
        check.Verdict("https://example.test/a.svg", "ok", "HTTP 200"),
        check.Verdict("https://example.test/b.svg", "broken", "HTTP 404."),
    ]
    text = check.report(verdicts, "gdmutant-0.1.0-py3-none-any.whl")
    assert "a.svg" in text and "b.svg" in text


# --- The wiring, which is where a guard is usually lost ------------------------------------------
# The code above can be perfect while the job sits outside `publish-pypi`'s `needs:` and gates
# nothing. That is a YAML fact, so it is asserted here rather than described in a comment.

_PUBLISH = REPO / ".github" / "workflows" / "publish.yml"


def _publish_workflow() -> dict:
    return yaml.safe_load(_PUBLISH.read_text(encoding="utf-8"))


def test_the_image_check_runs_in_the_publish_workflow() -> None:
    assert "readme-images" in _publish_workflow()["jobs"]


def test_the_image_check_blocks_the_upload() -> None:
    """A job whose `needs:` failed is skipped, and `publish-pypi` has no `if: always()` to override
    that -- so being in this list is exactly what makes it a gate rather than a report."""
    assert "readme-images" in _publish_workflow()["jobs"]["publish-pypi"]["needs"]


def test_the_image_check_waits_for_the_build_and_the_provenance_gate() -> None:
    """It reads the artifact `build` produced, and it can only pass once `provenance` has confirmed
    the tag the rewritten URL is pinned to."""
    needs = _publish_workflow()["jobs"]["readme-images"]["needs"]
    assert "build" in needs and "provenance" in needs


def test_the_image_check_skips_the_testpypi_rehearsal() -> None:
    """The dry-run builds from an untagged commit, so the tag-pinned URL 404s by design there. A
    check that is knowably wrong on a path teaches people to ignore it on every path."""
    assert _publish_workflow()["jobs"]["readme-images"]["if"] == "github.event_name == 'release'"


def test_the_image_check_reads_the_built_artifact_rather_than_the_repo() -> None:
    """Anchored on the actual `run:` line: the job's comment also mentions README.md when it
    explains the design, and matching prose would pass however the command is later changed."""
    steps = _publish_workflow()["jobs"]["readme-images"]["steps"]
    commands = [step["run"] for step in steps if "run" in step]
    assert any(
        "check_readme_images.py" in command and "--dist-dir" in command for command in commands
    )
    assert any(step.get("uses", "").startswith("actions/download-artifact@") for step in steps)


def test_the_image_check_holds_no_publishing_privilege() -> None:
    """The worst a bug in a guard should be able to do is refuse to publish."""
    assert _publish_workflow()["jobs"]["readme-images"]["permissions"] == {"contents": "read"}
