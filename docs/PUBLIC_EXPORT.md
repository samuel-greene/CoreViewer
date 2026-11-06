# Public export notes

This edition preserves the original development graph while removing private
content throughout its reachable branch history. Author names, author/committer
dates, commit ordering, branch tips, and merge parents are retained. Personal
emails are replaced; the portfolio author's address uses their verified GitHub
noreply identity. Original signatures cannot remain valid after rewriting.

Removed from the public folder and its imported Git history:

- Acquired images, generated research-image visualizations, and sidecars.
- Local catalog databases, Python environments, caches, and OS metadata.
- Internal operational notes, deployment receipts, and production runbooks.
- Cloud deployment automation containing organization-specific destinations.
- Experimental inference helpers containing local image paths.
- Original remotes, remote-tracking refs, local agent/checkpoint refs, reflogs,
  and unreachable objects from the private repository.

Organization-specific resource identifiers and upstream repository references
were replaced in retained historical content and commit messages. The event
consumer now requires an explicitly configured storage account. The GitHub
workflow runs tests and does not deploy to cloud resources.

The synthetic image generator and test fixtures use generated mathematical data.
Real images, local credentials, databases, and common output formats are ignored
by Git and excluded from the Docker build context. Ignore rules do not protect
files added with `git add --force` or data embedded directly in source files.

See [development history](HISTORY.md) for PR merge evidence. Hosted GitHub PR
pages, review comments, CI logs, secrets, and repository settings are not contained
in Git and are not recreated by this export.

Before adding future portfolio screenshots or sample metadata, use synthetic
inputs and check visible labels. Upload this folder's sanitized branches to
a new repository; do not fetch or merge the private repository into this copy.
