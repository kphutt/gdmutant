---
type: record
status: active
---
<!-- backlog-format: 3 -->

# gdmutant archive

Closed work, newest first. Open work is in [gdmutant-backlog.md](gdmutant-backlog.md). Written only by the maintainer's `chore(backlog):` PRs.

| # | Closed | Item | What wrapped up | Shipped in | Release |
|---|---|---|---|---|---|
| <a id="gm-3"></a>GM-3 | 2026-11-28 | Stop a timed-out command's whole process tree on Windows | On Windows, a timed-out command's process tree now stops, including a child left behind by a command that already finished. Thanks to @normanthesquid. | [#368](https://github.com/kphutt/gdmutant/pull/368) ([14f3e48](https://github.com/kphutt/gdmutant/commit/14f3e48aa8cb3d889f04a9ab4f873cb2110b5f2a)) | Unreleased |
| <a id="gm-2"></a>GM-2 | 2026-11-28 | Score a mutant invalid when Godot cannot compile its file | A mutant whose file Godot cannot compile now scores `invalid` instead of `error`. Thanks to @normanthesquid. | [#373](https://github.com/kphutt/gdmutant/pull/373) ([2e87d9b](https://github.com/kphutt/gdmutant/commit/2e87d9b04e4217cfb4058f49753b75df94acb7d0)) | Unreleased |
| <a id="gm-1"></a>GM-1 | 2026-11-28 | Start the backlog | The three backlog files were set up and seeded with the open work. | [#391](https://github.com/kphutt/gdmutant/pull/391) ([62f4024](https://github.com/kphutt/gdmutant/commit/62f40242d547d73d0027e19a4fa59f91c6fc3d98)) | Unreleased |

<!-- sample
Sample row (copy it for the first closed item; its anchor is the lowercase ID):

| <a id="gm-1"></a>GM-1 | YYYY-MM-DD | Move the backlog into the repository | One plain line on what changed. | [#NNN](https://github.com/kphutt/gdmutant/pull/NNN) ([abc1234](https://github.com/kphutt/gdmutant/commit/<full-hash>)) | Unreleased |

Release is optional, for a repo that tags releases. It reads Unreleased until a tag contains
the row's commit (`git merge-base --is-ancestor <commit> <tag>`), then links that version's
CHANGELOG heading. The CHANGELOG stays the release record; this file links to it and never
copies its text.

For an outside contributor's PR, the What wrapped up line also credits them by @handle.

Work with no PR names where the change can be seen, for example "Repository settings,
read back on YYYY-MM-DD".
-->
