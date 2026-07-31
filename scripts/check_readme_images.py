#!/usr/bin/env python3
"""Fail a release if any image in the *built* long description does not resolve.

WHAT THIS CATCHES, AND WHY NOTHING ELSE DOES. ``README.md`` is gdmutant's PyPI long description,
and the two surfaces resolve image paths differently: GitHub resolves a repo-relative ``src`` using
the viewer's session, while PyPI's renderer does not resolve relative paths at all -- it hands the
literal string to a camo proxy, which hex-encodes it and 404s. So a banner that looks perfect on
the repo front page can be a broken image on the project page, and nothing on the way there says
so. ``twine check`` will not: it validates that the metadata *parses and renders*, never that a URL
*resolves*. ``tests/test_packaging.py`` catches the static half (a relative path surviving the
substitution) without a network. This script catches the other half -- the URL is absolute and
well-formed and still does not exist.

READ THE BUILT DISTRIBUTION, NOT ``README.md``. Those deliberately differ. ``pyproject.toml``'s
``hatch-fancy-pypi-readme`` hook rewrites the banner's relative ``src`` into an absolute,
tag-pinned ``raw.githubusercontent.com`` URL at build time, so the file on disk and the string PyPI
receives are not the same text. Checking the source file would test a string no user will ever see.

WHERE THIS RUNS, AND WHY IT CAN ONLY RUN THERE. The rewritten URL contains the release tag
(``.../v0.1.0/...``), so it cannot resolve until that tag exists on GitHub. That rules out every
earlier point in the pipeline: a pre-tag check would fail on a URL that is not wrong, only early,
and a gate that always fails gets deleted. ``publish.yml`` runs it after ``provenance`` has proved
the tag names this commit and after ``build`` has produced the artifact, but *before*
``publish-pypi`` is granted its OIDC token -- the last moment where failing is still free. A PyPI
version number can never be reused and a long description is frozen at upload, so afterwards there
is no fix, only a new version.

Usage::

    python3 scripts/check_readme_images.py --dist-dir dist

Exit codes: 0 every image resolves; 1 an image is genuinely broken (fix the README, then cut a new
version); 2 usage or no distribution found; 3 the network could not answer (nothing is known to be
wrong -- re-run the job).
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from email import message_from_string
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit

#: Exit codes, named so the workflow log and the tests agree on what each one means.
OK = 0
BROKEN = 1
USAGE = 2
UNVERIFIED = 3

#: Statuses worth another attempt: the server is up but is not answering the question right now.
#: A 404 is deliberately absent -- it is a fact, not a hiccup, and retrying it only wastes a minute
#: before reporting the same thing.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Hosts where a 404 has a specific, common, non-obvious cause worth spelling out.
GITHUB_HOSTS = frozenset(
    {"github.com", "raw.githubusercontent.com", "objects.githubusercontent.com"}
)

_GITHUB_404_HINT = (
    " A 404 from GitHub usually means one of three things: the tag is not pushed yet, the file is "
    "not in the tagged commit, or the repository is still private - an anonymous fetch of a "
    "private repo's asset 404s, and anonymous is exactly what PyPI's camo proxy is."
)

#: `![alt](url)` and `![alt](<url> "title")`. The URL stops at whitespace so a title is not
#: swallowed into it.
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")

#: An `<img>` or `<source>` tag, whole, so attributes can be pulled out of it individually.
_IMAGE_TAG = re.compile(r"<(?:img|source)\b[^>]*>", re.IGNORECASE | re.DOTALL)

_SRC_ATTR = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SRCSET_ATTR = re.compile(r"""\bsrcset\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


@dataclass(frozen=True)
class Response:
    """What a fetcher reports back. `error` is non-empty only when the request never completed."""

    status: int | None = None
    content_type: str = ""
    final_url: str = ""
    error: str = ""


@dataclass(frozen=True)
class Verdict:
    """One image URL's outcome. `state` is "ok", "broken" or "unverified"."""

    url: str
    state: str
    detail: str


#: A fetcher: given a URL, report what the server said. Injected so every rule below is unit-
#: testable with no network -- a gate nobody can test offline is a gate nobody edits with
#: confidence.
Fetcher = Callable[[str], Response]


def long_description(dist_dir: Path) -> tuple[str, str]:
    """The long description out of a built wheel or sdist, with the name of the file it came from.

    The wheel is preferred purely because it is cheaper to open; both carry the same metadata, and
    both are produced by the same `uv build` the release actually uploads.
    """
    for wheel in sorted(dist_dir.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.endswith(".dist-info/METADATA"):
                    raw = archive.read(name).decode("utf-8")
                    return message_from_string(raw).get_payload(), wheel.name
    for sdist in sorted(dist_dir.glob("*.tar.gz")):
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                if member.name.endswith("/PKG-INFO"):
                    handle = archive.extractfile(member)
                    if handle is not None:
                        raw = handle.read().decode("utf-8")
                        return message_from_string(raw).get_payload(), sdist.name
    raise FileNotFoundError(
        f"no built wheel or sdist with readable metadata in {dist_dir} - "
        "run `uv build` first, or point --dist-dir at the downloaded artifact"
    )


def image_urls(text: str) -> list[str]:
    """Every image URL the long description references, in order, without duplicates.

    Markdown images, `<img src>`, and `<picture>`'s `<source srcset>` -- the three ways an image
    can reach the rendered page. Deduplicated because the same badge appearing twice is one
    question, not two, and asking a badge host the same thing twice is just rudeness.
    """
    found: list[str] = []
    found.extend(_MARKDOWN_IMAGE.findall(text))
    for tag in _IMAGE_TAG.findall(text):
        found.extend(_SRC_ATTR.findall(tag))
        for srcset in _SRCSET_ATTR.findall(tag):
            # `url 2x, other.png 400w` -- take each candidate's URL, drop its descriptor.
            found.extend(part.split()[0] for part in srcset.split(",") if part.split())
    ordered: list[str] = []
    for url in found:
        if url not in ordered:
            ordered.append(url)
    return ordered


def static_verdict(url: str) -> Verdict | None:
    """A verdict reachable without asking the network, or None if the URL must be fetched."""
    scheme = urlsplit(url).scheme.lower()
    if scheme in ("http", "https"):
        return None
    if not scheme:
        return Verdict(
            url,
            "broken",
            # ASCII only: this string is printed, and gdmutant already shipped a Windows bug where
            # console output crashed under the legacy cp1252 code page. A guard that crashes
            # instead of reporting the problem is worse than no guard.
            "not an absolute URL. PyPI does not resolve a path against the repository the way "
            "GitHub does - its camo proxy hex-encodes the literal string and 404s, so this renders "
            "as a broken image on the project page.",
        )
    return Verdict(
        url,
        "broken",
        f"scheme {scheme!r} is not http or https. readme_renderer's sanitiser allows only http, "
        "https and mailto, so PyPI strips the image out of the page entirely. (This is why a "
        "data: URI is not an option here.)",
    )


def interpret(url: str, response: Response) -> Verdict:
    """Turn one server answer into a verdict.

    The split that matters: "this image is broken" and "the network did not answer" are different
    facts and get different exit codes, because they need different actions -- one is a README fix
    and a burned version number, the other is a re-run.
    """
    if response.error:
        return Verdict(url, "unverified", f"request failed: {response.error}")
    if response.status is None:
        return Verdict(url, "unverified", "no status returned")
    if response.status in RETRYABLE_STATUSES:
        return Verdict(url, "unverified", f"HTTP {response.status} (server busy or unavailable)")
    if 300 <= response.status < 400:
        return Verdict(url, "broken", f"HTTP {response.status}: redirect was not followed")
    if response.status >= 400:
        hint = _GITHUB_404_HINT if urlsplit(url).netloc.lower() in GITHUB_HOSTS else ""
        return Verdict(url, "broken", f"HTTP {response.status}.{hint}")
    if response.content_type.split(";")[0].strip().lower() == "text/html":
        return Verdict(
            url,
            "broken",
            "HTTP 200, but the response is an HTML page, not an image - camo will proxy the page "
            "and the browser will show a broken image. A `.../blob/...` GitHub link does this; the "
            "`raw.githubusercontent.com` form of the same file is what serves the bytes.",
        )
    followed = f" (redirected to {response.final_url})" if _redirected(url, response) else ""
    return Verdict(url, "ok", f"HTTP {response.status}{followed}")


def _redirected(url: str, response: Response) -> bool:
    """Whether the fetcher ended up somewhere other than where it was asked to go."""
    return bool(response.final_url) and response.final_url != url


def check_url(
    url: str,
    fetch: Fetcher,
    attempts: int = 3,
    delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Verdict:
    """Resolve one image URL, retrying only what is worth retrying.

    A release must not fail because a badge host had a bad second, so a transient answer is tried
    again with a lengthening pause. A 404 is not retried: it is already the answer.
    """
    static = static_verdict(url)
    if static is not None:
        return static
    verdict = Verdict(url, "unverified", "not attempted")
    for attempt in range(1, attempts + 1):
        verdict = interpret(url, fetch(url))
        if verdict.state != "unverified":
            return verdict
        if attempt < attempts:
            sleep(delay * attempt)
    return verdict


def check_all(
    urls: Iterable[str],
    fetch: Fetcher,
    attempts: int = 3,
    delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Verdict]:
    return [check_url(url, fetch, attempts, delay, sleep) for url in urls]


def urllib_fetcher(timeout: float = 15.0) -> Fetcher:
    """The real fetcher: a HEAD, falling back to GET where a host refuses HEAD.

    Redirects are followed (urllib's default, capped at 10 hops) because camo follows them too --
    a URL that redirects to a real image is a working image, and calling it broken would be wrong.
    The final URL is reported so a surprising hop shows up in the log rather than passing silently.
    """

    def fetch(url: str) -> Response:
        for method in ("HEAD", "GET"):
            headers = {"User-Agent": "gdmutant-release-gate (+https://github.com/kphutt/gdmutant)"}
            try:
                with request.urlopen(
                    request.Request(url, method=method, headers=headers), timeout=timeout
                ) as response:
                    return Response(
                        status=response.status,
                        content_type=response.headers.get("Content-Type", ""),
                        final_url=response.url,
                    )
            except error.HTTPError as http_error:
                # Some CDNs answer HEAD with 403/405/501 and serve the same URL happily on GET.
                # Retrying once with GET keeps those from being reported as broken images.
                if method == "HEAD" and http_error.code in (403, 405, 501):
                    continue
                return Response(
                    status=http_error.code,
                    content_type=http_error.headers.get("Content-Type", ""),
                    final_url=http_error.url or url,
                )
            except Exception as failure:  # noqa: BLE001 - any transport failure is "unverified"
                return Response(error=f"{type(failure).__name__}: {failure}")
        return Response(error="HEAD was refused and GET was never reached")  # pragma: no cover

    return fetch


def report(verdicts: list[Verdict], source: str) -> str:
    """The whole result as one block of text, so a CI log shows every URL, not only the first
    bad one."""
    lines = [f"Images in the built long description ({source}):", ""]
    marks = {"ok": "ok      ", "broken": "BROKEN  ", "unverified": "UNKNOWN "}
    for verdict in verdicts:
        lines.append(f"  {marks[verdict.state]}{verdict.url}")
        lines.append(f"            {verdict.detail}")
    return "\n".join(lines)


def main(
    argv: list[str], fetch: Fetcher | None = None, sleep: Callable[[float], None] = time.sleep
) -> int:
    parser = argparse.ArgumentParser(
        prog="check_readme_images.py", description=__doc__, allow_abbrev=False
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=Path("dist"),
        help="directory holding the built wheel/sdist (default: dist)",
    )
    parser.add_argument("--attempts", type=int, default=3, help="tries per URL (default: 3)")
    parser.add_argument(
        "--delay", type=float, default=2.0, help="seconds before the first retry (default: 2)"
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="per-request timeout in seconds (default: 15)"
    )
    args = parser.parse_args(argv[1:])

    try:
        text, source = long_description(args.dist_dir)
    except (FileNotFoundError, OSError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return USAGE

    urls = image_urls(text)
    if not urls:
        # Refusing to report success on nothing. The README has images; finding none means the
        # extraction broke, and a green check on zero URLs is exactly the silent pass this exists
        # to prevent.
        print(
            f"error: no image URLs found in the long description from {source}. The README has "
            "images, so this means the extraction is broken - not that everything resolves.",
            file=sys.stderr,
        )
        return BROKEN

    verdicts = check_all(
        urls, fetch or urllib_fetcher(args.timeout), args.attempts, args.delay, sleep
    )
    print(report(verdicts, source))

    broken = [v for v in verdicts if v.state == "broken"]
    unverified = [v for v in verdicts if v.state == "unverified"]
    if broken:
        print(
            f"\nerror: {len(broken)} image(s) in the PyPI long description do not resolve. "
            "A long description is frozen at upload and a version number can never be reused, so "
            "fix the README (or the substitution in pyproject.toml) and cut a new version.",
            file=sys.stderr,
        )
        return BROKEN
    if unverified:
        print(
            f"\nerror: {len(unverified)} image(s) could not be checked after {args.attempts} "
            "attempts. Nothing is known to be wrong with the README - this is a network or host "
            "problem. Re-run this job from the Actions tab.",
            file=sys.stderr,
        )
        return UNVERIFIED
    print(f"\nall {len(verdicts)} image(s) in the long description resolve")
    return OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
