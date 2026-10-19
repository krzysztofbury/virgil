# Releases

Version 0.7.0 consolidates verification, qualifies deployable artifacts, and adds
operator recovery. The package version and dated changelog entry must match.

## Branch and PR

1. Implement and verify the change on one feature branch.
2. Run the checks in [CONTRIBUTING.md](CONTRIBUTING.md), including the image smoke
   test for runtime, dependency, migration, or Docker changes.
3. Open a PR. Both `ci` and `image` must succeed. Before merging, compare the
   remote PR head from `gh pr view --json headRefOid` with the pushed revision.

CI uses PR events rather than also running on feature-branch pushes. Local checks
remain available before opening a PR. Manual CI dispatch verifies the selected
branch; only main can publish the production pointer.
Weekly verification reruns tests and vulnerability audits without publishing.

## Main publication

The main push runs locked verification and audits, builds one image, and starts
that image with an isolated volume and no external network. Signup, migrations,
templates, static assets, restart, and login after offline recovery must work.
The same image is then pushed as
`sha-<full-commit>`, `sha-<short-commit>`, and `latest`.

Only the publishing job receives package-write permission. Main runs share a
concurrency group so superseded work is cancelled. Watchtower remains best-effort
auto-update; a successful registry push is not proof of a healthy deployment.
Confirm the running version and `/healthz` through the authorized deployment path.
Load-sensitive changes also need representative load verification.

## Version aliases and GitHub releases

After the main image has been published:

1. Verify the remote main revision and the successful image job.
2. Create `vMAJOR.MINOR.PATCH` on that commit and push the tag.
3. The tag job checks the version, dated changelog, and main ancestry. It aliases
   the existing `sha-<full-commit>` image to `MAJOR.MINOR.PATCH`. A missing image
   fails the job; there is no untested rebuild or implicit `latest` tag.
4. Publish the GitHub release using that version's changelog as its notes.

Tags containing the new workflow cannot update `latest`. Do not tag historical
commits containing the retired release workflow: GitHub executes workflows from
the tagged revision. After merging this change, disable the old `Release` workflow
in GitHub Actions before creating historical tags. Commits from before the full
SHA image convention have no qualified alias source. Do not move published tags.

## Rollback

Pause Watchtower before rollback. Select the known-good `sha-*` image explicitly
in Compose rather than retagging a local image as `latest`. Check the migration
boundary before starting an older image: image rollback cannot reverse database
migrations, and an older image refuses a database migrated past it (its
`/healthz` stays red, and autoheal keeps restarting it). Use matching pre-migration snapshots or the complete recovery process
in [docs/recovery.md](docs/recovery.md), preserving current data for investigation.
Re-enable automatic updates only after a correcting main image is verified.
