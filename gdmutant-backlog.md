---
type: record
status: active
created: 2026-11-27
---

# gdmutant backlog

Open work for gdmutant. Closed work is in [gdmutant-archive.md](gdmutant-archive.md). Facts git can't show are in [gdmutant-notes.md](gdmutant-notes.md).

## Links

- Repo: https://github.com/kphutt/gdmutant
- Board: this file
- Releases: [CHANGELOG.md](CHANGELOG.md)
- Ask for work: [open an issue](https://github.com/kphutt/gdmutant/issues/new/choose)

## How this file works

<!-- backlog-rules v2. -->

- One writer. Only the backlog step changes this file, the archive and the notes. It runs from `main` after merges, as its own small PR, one at a time. Feature PRs, contributor PRs included, never edit these three files.
- Closing. A PR names the item it finishes with a `Closes: GM-12` line in its body, or `Closes: none`. After merge, the backlog step moves the row to the archive with the PR, the commit and, once a release contains it, the version.
- IDs. Every item has a permanent ID that is never reused. The `GM-` prefix keeps GitHub from reading it as an issue or PR number. Only the backlog step assigns IDs, from `Next ID` below, and raises it in the same commit.
- Order. A row's position is its order. The next item is the top row of In progress, or else the top row of Ready to build.
- Sections are status. In progress: an open PR, draft allowed, carries the item's `Closes:` line. Blocked: waiting on something named in Next step, since the date in Since. Ready to build: defined, with a Done when. Needs definition: not yet ready to build.
- Work with no PR, such as a settings change, stays in Ready to build until the backlog step archives it. A settings row closes only after the setting is read back.
- Who. `agent`: any agent may pick it up. `maintainer`: it needs the maintainer, for example a secret, an account setting or a call on taste.
- Effort. S: under an hour. M: a few hours. L: a day or more. ?: not sized yet.
- Public. This file is public. It never holds an unfixed security issue, a private link, personal data or a local path. People appear only by @handle.
- Seeding. A backlog PR from `main` may add or re-plan many rows at once. It follows every other rule here.
- Asking for work. Open a GitHub issue. Accepted issues become rows that cite the issue number.

Next ID: GM-2

## In progress

| # | Item | Who | Effort | Description | Next step | Done when |
|---|---|---|---|---|---|---|

## Blocked

| # | Item | Who | Effort | Description | Next step | Done when | Since |
|---|---|---|---|---|---|---|---|

## Ready to build

| # | Item | Who | Effort | Description | Next step | Done when |
|---|---|---|---|---|---|---|
| GM-1 | Start the backlog | agent | M | The three files, seeded with the open work. | Open the seeding PR with `Closes: GM-1` | `uv run python scripts/check_backlog.py gdmutant GM` exits 0 with the open work in the tables |

## Needs definition

| # | Item | Who | Effort | Description | Next step | Done when |
|---|---|---|---|---|---|---|

<!-- Optional section:
## Research

Each item ends in tests or a written result, not a feature. Same columns as above.
-->
