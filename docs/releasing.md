---
type: how-to
status: active
created: 2026-07-23
---

# Releasing gdmutant

How gdmutant's distributions reach PyPI. Publishing uses Trusted Publishing (OIDC), so no API
token is stored in the repo. Design and rationale:
[`docs/decisions/0010-pypi-trusted-publishing.md`](decisions/0010-pypi-trusted-publishing.md).

Two workflows split the work, with a human standing between them:

- [`.github/workflows/release.yml`](../.github/workflows/release.yml) triggers on a pushed version
  tag (`v*.*.*`). It checks the tag against the version in `pyproject.toml` and the tagged commit
  against `main`, then creates a draft GitHub Release with auto-generated notes. It never creates
  or moves a tag (`gh release create --verify-tag` fails if the tag is missing), and it uploads
  nothing.
- [`.github/workflows/publish.yml`](../.github/workflows/publish.yml) triggers on a published
  Release. It builds the sdist + wheel with `uv build`, validates them with `twine check`, runs the
  full release gate, and uploads via `pypa/gh-action-pypi-publish` using a short-lived OIDC
  credential.

A draft Release does not fire `release: published`, so pushing a tag stages a release and stops
there. The upload waits for a maintainer to open the draft on GitHub and press Publish, the
deliberate, auditable human act ADR-0010 gates a real upload on.

## Prerequisite: the one-time manual seed (maintainer)
Every publish (dry-run or real) depends on a trusted publisher registered on each index. Registering
one is a web-login step only the account owner can do. Values (full table in the ADR):

- https://pypi.org and https://test.pypi.org -> Account settings -> Publishing -> add a
  GitHub publisher: Owner `kphutt`, Repository `gdmutant`, Workflow `publish.yml`, Environment `pypi`
  (on PyPI) / `testpypi` (on TestPyPI), PyPI Project Name `gdmutant`.
- Create the GitHub Environments `pypi` and `testpypi` under repo Settings -> Environments.

A publish that fails at the publish step with an OIDC-trust error means this registration is
missing or does not match the workflow, environment and repository it names.

### The publisher moves after an index's first upload
An index that has never received a `gdmutant` upload has no project to hang a publisher on, so the
registration above is a pending publisher, held against the *account*. The first successful
upload creates the project and [converts the pending publisher into a normal
one](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/), which then lives
under the *project's* Settings -> Publishing and is gone from the account-level pending list.

That relocation is the trap. After a release the account page that used to list the publisher is
empty, and adding a pending publisher there again does nothing, because the project exists now.
Look at the *project's* publishing settings instead, and confirm the entry is there. If it isn't,
add it there with the same five values. Otherwise the *next* release fails at the OIDC step with a
trust error whose real cause, an earlier upload that succeeded, appears nowhere in the log.

## Dry-run -> TestPyPI (`workflow_dispatch`)
Rehearse the full OIDC + upload path against the throwaway index without cutting a release:

- GitHub -> Actions -> Publish -> Run workflow (on the branch/tag you want to build).
- Or from the CLI: `gh workflow run publish.yml`.

This runs the `build` job then `publish-testpypi` (uploading to `https://test.pypi.org/legacy/`).
Verify the result at https://test.pypi.org/p/gdmutant.

## Real release -> PyPI (push a tag, then press Publish)

1. Set the version in `pyproject.toml`. The tag must match it exactly.
   `scripts/check_release_tag.py` fails the release if it doesn't.
2. Date the changelog. Change this version's heading in `CHANGELOG.md` from
   `## [X.Y.Z] — unreleased` to `## [X.Y.Z] — YYYY-MM-DD`, using the date you expect to publish.
   Nothing automates this and no check enforces it, and it has to happen before the tag: the tag
   ships the commit it points at, so tagging first publishes a changelog that calls the shipped
   version unreleased.
3. Merge both to `main` through the usual PR. A tag whose commit is not an ancestor of `main` is
   refused.
4. Push the tag: `git tag vX.Y.Z <commit>` then `git push origin vX.Y.Z`. Get it right the first
   time. The repo's tag ruleset blocks deleting and re-pointing tags for anyone acting normally —
   `current_user_can_bypass: never`, confirmed via the API — so a tag naming the wrong version or
   commit cannot be fixed in place through the ordinary push/delete path; the default recovery is
   to burn the version number and cut a new one. A repo admin *can* still disable the ruleset
   itself (a separate, deliberate config change, not a bypass of the active rule), delete the tag,
   and re-enable it — but treat that as a rare, considered override, only for a tag nothing
   external has ever referenced (nothing published, no clone or consumer could have pinned to it),
   never as a routine fix.
5. *Automatic.* `release.yml` runs its two guards (the tag matches the packaged version, and the
   tagged commit is on `main`) and then stages a draft Release with generated notes. A guard
   that fails leaves no Release at all, so nothing has shipped.
6. Review the draft on GitHub and press Publish. The generated notes are a raw commit list. Edit
   them into something worth reading, with this version's `CHANGELOG.md` entry as the source.
   Publishing is what fires `publish.yml`, and only then does anything reach PyPI.
7. *Automatic.* `publish.yml` runs the release gate. Remember the structure rather than a list of
   job names: every job in `publish-pypi`'s `needs:` is a guard, each one is a hard dependency with
   no `if: always()` to soften it, and GitHub skips a job whose `needs:` failed or was itself
   skipped, so a red guard stops the upload before the OIDC token is minted and no guard can be
   waved through ([ADR-0012](decisions/0012-merge-time-local-ship-time-cloud.md)). The workflow's
   header comment names each guard and says what it is for, and
   `gh run view --workflow publish.yml` shows the live set with the result of each. Real Godot runs
   in there, so this is a slow run, not a quick one. A guard that fails stops the upload while
   leaving the Release published. Fix the cause and re-run the failed jobs from the Actions tab
   (a re-run replays the same `release: published` event), or cut a new version if the fix needs a
   code change.
8. *Automatic, and after the upload.* `verify-published` installs the released version from the
   index and runs it. It sits outside the gate on purpose, so a red result there reports on a
   version that is already public rather than stopping a release. Details are under
   [Recurring](#recurring-every-release), item 1.
9. Verify what shipped. The project page is at https://pypi.org/p/gdmutant, and the checklist
   below covers what a green upload does not prove.

## After the release
Almost everything that can be wrong with a release is invisible from the inside. The maintainer's
browser is signed in, their machine already has the source, and their clone already has the tag, so
the page 404s for a stranger, the install pulls files nobody else receives, and the `uses:` line
resolves to a ref only this account can see. A green publish run proves more than a successful
upload. It proves the package installs from the index and runs there, and that every image in the
long description resolves. What it cannot prove is what a stranger sees: a signed-out project page,
a published action ref resolving from somebody else's repository, and the links and badges on the
repository front page.

Each item below is one action and the result that counts as a pass. Recurring items belong to
every release. One-time items are setup: do them once, confirm them once, and never re-check them
at release time. Where a job in `publish.yml` already answers an item, the item says so and the
manual version becomes the way to reproduce a failure by hand.

### Recurring: every release

1. Install from the index on a machine that has never held the source.

   ```sh
   uv tool install gdmutant     # or, in a fresh virtualenv: pip install gdmutant
   gdmutant --version
   ```

   *Pass:* the printed version is the one just tagged, and running `gdmutant example` followed by
   `gdmutant run gdmutant-hello-world.gd --dry-run` in an empty folder lists mutants. That proves
   the entry point, the declared dependencies, the packaged example file, and the parser all
   arrived.

   An editable install, a `pip install dist/*.whl`, or any command run from a checkout proves none
   of it, because each reads files a stranger never receives. The install has to resolve by name,
   from the index. Index propagation takes a few minutes, so a `404` straight after the upload means
   wait, not fail. *Automated:* `publish.yml`'s `verify-published` job does this for every release.
   It installs the released version from the index into a throwaway virtualenv outside the
   workspace, retries while the index propagates, asserts the version, asserts that `gdmutant`
   imported from that virtualenv rather than from a checkout on the path, and mutates a scratch file
   with `--dry-run`. Run the commands above by hand when that job goes red and you want to watch the
   failure yourself. It is not a gate and no ordering can make it one: it has to run after the
   upload, because the only way to ask what the index serves is to publish first, and by then the
   version number is spent. Its own comment in `publish.yml` says exactly that, so nobody reads the
   green check as permission.

2. Open the project page signed out. https://pypi.org/p/gdmutant in a private window.

   *Pass:* the banner image renders rather than showing a broken-image icon, the description is
   formatted Markdown rather than raw text, the version shown is the new one, and every entry in the
   sidebar's project links (Homepage, Repository, Issues, Changelog) opens.

   PyPI renders the description with its own renderer and fetches images through its own proxy, so a
   banner that is fine on the repo front page can still fail here. A release's description is
   frozen at upload, so the only fix for a broken one is another version number. `twine check`, in
   the build job, proves the description *renders*. It never fetches an image. *Automated:*
   `publish.yml`'s `readme-images` job pulls the image URLs out of the built distribution's
   long-description metadata, not out of `README.md`, which deliberately differs because the build
   rewrites the banner into a tag-pinned absolute URL. A non-200 fails the job, and the job is in
   `publish-pypi`'s `needs:`, so this half really is a gate. The rest of the pass line above, the
   formatting and the version and the sidebar links, is still a look.

3. Run the action from a consumer's seat. In a *separate* repository, add a workflow that pins
   the published ref the action's documentation tells consumers to use, passes the documented inputs,
   and appends `--html report.html` through the `args` input, then upload that file as an artifact.

   *Pass:* the ref resolves (a `uses:` naming a ref that does not exist fails the run before any
   step executes), the run completes, the job summary carries the survivor block, and the downloaded
   `report.html`, opened on a machine with the network switched off, shows every survivor with its
   explanation.

   The in-repo smoke workflow cannot answer this. It consumes the action as `uses: ./` and installs
   gdmutant from the branch under test, so it exercises the composite steps while proving nothing
   about a published ref resolving for somebody else, or about what the published package renders.

4. Look at the repository the way a stranger does. Front page, private window.

   *Pass:* the banner renders, every badge shows a real value rather than "no status", "invalid" or
   "not found", the README's documentation links all open, and Issues -> New issue offers its
   contact links, each of which opens.

   Then take the `more` URL out of a survivor block the CLI actually printed and paste it into the
   same private window. *Pass:* it lands on the survivor reference, at that operator's section. Every
   user sees that URL on every run, which makes it the most-read link the project has.

   A badge reading "no status" is reporting a workflow that has no automatic trigger, not a check
   that failed. The fix is "`ci.yml` runs automatically again", the last item under
   [One-time](#one-time-setup-confirmed-once).

### One-time: setup, confirmed once

- The trusted publisher moved. The first upload to an index converts the pending publisher and
  relocates it to the project's own publishing settings. Right after that first upload, separately
  for each index, confirm the entry is there:
  [The publisher moves after an index's first upload](#the-publisher-moves-after-an-indexs-first-upload).
  *Pass:* the five registration values are listed under the project's settings. Skipping this is
  invisible now and surfaces at the *next* release as an OIDC trust error whose real cause, an upload
  that succeeded, appears nowhere in the log.

- Private vulnerability reporting is on. `SECURITY.md` sends reporters to GitHub's private
  advisory form, and that form exists only where the setting is enabled. It is an option for public
  repositories, found in the repository's settings among the security options. *Pass:*
  `gh api repos/<owner>/<repo>/private-vulnerability-reporting --jq .enabled` prints `true`, and the
  repository's Security tab offers "Report a vulnerability". Judge the body, never the status code.
  On a public repository that endpoint answers either way, returning `{"enabled": false}` when the
  setting is off, so treating any non-404 as a pass reports success whether the form exists or not.
  A `404` there is a different fact altogether, that the repository is private. Nothing ever signals
  this is broken: a reporter who finds no form does not fall back to email, they give up quietly.
  *Automatable:* `scripts/harden_github.py` already converges repository settings through `gh api`,
  and this belongs in it.

- Discussions are enabled. The issue chooser's community-support link points at the repository's
  Discussions and 404s while they are off. *Pass:* `gh api repos/<owner>/<repo> --jq .has_discussions`
  prints `true`, and the link in the chooser opens a Discussions page. *Automatable:* same script,
  because `has_discussions` is a plain repository field.

- Description, topics, social preview. *Pass:* the repository header carries the one-line
  description and topics, and pasting the repository URL into a chat client produces a card showing
  the project's own image rather than a generic avatar. Topics are how a search for mutation testing
  or Godot reaches the project at all. Without them it is findable only by people who already know
  its name. *Automatable:* description and topics are repository settings `scripts/harden_github.py`
  can converge. The social-preview image is uploaded by hand and stays a one-time click. The
  source SVG and the exact PNG uploaded are `.github/assets/social-preview.svg`/`.png` -- committed
  so the design survives if the machine that made it doesn't. To regenerate the PNG after an SVG
  edit: open the SVG in a browser at its native 1280x640 and save a screenshot, or use any
  SVG-to-PNG renderer that respects the embedded `font-family` stack (the text is live system
  fonts, not outlined paths, so rendering fidelity depends on what fonts are installed on the
  machine doing the rendering).

- `ci.yml` runs automatically again. The README's CI badge reports on `ci.yml`, and a workflow
  with no automatic trigger has no result to report, so the badge reads "no status" to every
  visitor, the thing item 4 above catches without saying what to do about it. Restoring the triggers belongs
  to the move to public, where the reason they were removed, billed Actions minutes on a private
  repository, stops applying. The steps live in
  [ADR-0012](decisions/0012-merge-time-local-ship-time-cloud.md)'s Decision section, under "Trivial to
  reverse, by design". Follow them there rather than from here, so the two cannot drift. *Pass:*
  signed out, the badge on the repository front page shows a real result, passing or failing, instead
  of "no status". One knock-on to know about: this changes which checks report on a pull request, and
  `scripts/harden_github.py` converges branch protection off the back of that. The script carries no
  hand-written list. It reads the workflow files, derives each context string including the matrix
  suffixes GitHub appends, and refuses to require a job whose workflow no pull request triggers, so
  the usual version of this mistake is caught for you. Run
  `uv run python scripts/harden_github.py --check` after the change and read what it reports anyway,
  because the stake is worth a second look: a required check that nothing reports blocks every pull
  request forever.

- Secret scanning and push protection are on. Both are GitHub Advanced Security features that are
  free on a public repository but unavailable while it stays private, so they cannot be turned on
  ahead of time, only right after the flip. *Pass:* `uv run python scripts/harden_github.py --check`
  reports `secret_scanning` and `secret_scanning_push_protection` both `enabled`.
  *Automatable:* `scripts/harden_github.py` (no `--check`) already converges both.

### What is still fixable afterwards
- The GitHub Release's title and notes: editable at any time.
- The repository's docs, including the survivor-reference URL the CLI prints. It tracks `main`, so a
  fix there reaches every copy already installed.
- A published distribution and the description on its project page: not editable. A release can be
  yanked, which hides it from resolvers without deleting it, and replaced under a new version number.
  Nothing else.

## Notes
- Nothing is stored. There is no token secret, and the index mints a one-shot credential per upload.
- The build backend runs only in the low-privilege `build` job. The OIDC token is granted only to the
  publish jobs, each gated behind a GitHub Environment.
- Creating *and* publishing a Release straight from the GitHub web UI skips `release.yml` entirely.
  `publish.yml`'s gate still runs, which is why it repeats the provenance guards rather than trusting
  that the tag path already ran them.
- To reproduce the build locally: `uv build` then `uv run --with twine twine check dist/*`.
