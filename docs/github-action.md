---
type: how-to
status: active
created: 2026-08-03
---

# The gdmutant GitHub Action

Full reference for `kphutt/gdmutant`, the Action wrapping the CLI. For the minimal steps to add
it to a workflow, see the README's [GitHub Action](../README.md#github-action) section. This page
holds the detail that doesn't need to be in front of every reader.

## What it does

It sets up Python and Godot, installs gdmutant, runs it, and writes every survivor (with its
`gap` / `risk` / `start` explanation) to the workflow's job summary, where reviewers already look
(`job-summary: false` skips that). The `report-json` output holds the path to the
`mutation-testing-elements` report, ready to hand to an upload-artifact step.
`godot-use-dotnet: true` picks the .NET build of Godot. Survivors are output, not failure: the step
exits non-zero only on a real error, such as a red baseline suite. The project and a suite that
already passes must be there, plus the GUT or gdUnit4 addon if you use either. The action installs
none of that.

## Inputs

```yaml
- uses: kphutt/gdmutant@REPLACE_WITH_THE_RELEASE_COMMIT_SHA  # v0.1.0
  with:
    godot-version: "4.7.0"      # the only required input
    project-path: ./            # gdmutant's --project
    paths: scripts              # what to mutate (default: the whole project)
    runner: gdunit4             # gdunit4 | gut | command
    tests: res://test/unit      # the one directory holding your suites
    since: ${{ github.event.pull_request.base.sha }}   # mutate only this PR's changed lines
    args: --jobs 4              # any extra gdmutant flags, verbatim
```

`since` reads the base commit out of your clone, so the workflow's `actions/checkout` step needs
`fetch-depth: 0`. Its default fetches one commit, the base commit is not among them, and the
gdmutant step then fails on a git error rather than mutating anything.

If your project's `.gdmutant.toml` sets `command` or `godot`, add `args: --trust-config`. Without
it gdmutant refuses to run at all and the step fails, for the reason given under the README's
[Configuration](../README.md#configuration) section.

## Pinning

There is no `@v1` or `@v0`. Every published tag names a full version (`v0.1.0`) and never moves: a
tag ruleset blocks deleting or re-pointing any tag, and the release guard rejects a tag that
doesn't equal the packaged version, so a floating major tag is not something this repo can produce.
Pinning `@v0.1.0` works and is just as stable, for the same reason. Replace the placeholder above
with the 40-character commit SHA the release was cut from, and keep the version in the trailing
comment so the line stays readable.

The bumps a floating tag would have handed you come from Dependabot instead, as PRs you can read
before taking:

```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
```
