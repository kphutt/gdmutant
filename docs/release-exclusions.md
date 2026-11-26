---
type: record
status: active
created: 2026-11-26
---

# Pull requests left out of the release notes

Pull requests users never see, so no release note mentions them. The release-notes check reads
this list.

- #374: ruff bump, a development tool only.
- #376: action pins and the CHANGELOG reset after 0.1.7.
- #380: wording of a note in this repository's own CI workflow.
- #381: wcmatch security update. wcmatch is used only by poodle, a development tool, so installs
  of gdmutant never had it.
- #383: the release workflows now require a green release rehearsal. Maintainers only.
