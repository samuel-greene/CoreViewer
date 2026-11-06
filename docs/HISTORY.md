# Development history

The sanitized history retains **56 original commits**, **two branches** (`main`
and `sql_dev_branch`), and **nine merge commits**, including the six PR merges
listed below. The original development graph, author names, and author/committer
timestamps are preserved. A later public-export commit documents the sanitation.

## Pull request merge evidence

These entries come from the repository's merge commits. Numbers refer to the
original upstream project; they do not create PR pages in a new GitHub repository.
The commit identifiers below refer to sanitized history and can be inspected with
`git show <commit>` or `git log --all --graph`.

| Original PR | Merge date (author timezone) | Recorded title | Sanitized merge commit |
| --- | --- | --- | --- |
| #8 | 2026-10-13 | Added a soft build timestamp to login screen to assist with debugging build problems and version control | `ba6933c0a3c2` |
| #5 | 2026-10-07 | Allow Incoming JP2 folders for manual OME export and inference | `6afbaf9448a7` |
| #4 | 2026-10-07 | Offer CycleGAN virtual H&E for selected incoming scan folders | `b09b15511d1d` |
| #3 | 2026-10-07 | Enable guest conversions with a configurable permission switch | `df0ec2d05f78` |
| #2 | 2026-10-07 | Connect to the private OME generator without a shared API key | `5a864e6a6a99` |
| #1 | 2026-10-07 | Use Incoming for manual OME conversion and show real processing status | `a659a49e094a` |

## Attribution and privacy

Original author names and their existing aliases are retained, including other
contributors. No work has been reassigned to the portfolio author. That author's
personal email was replaced by the noreply address for their verified GitHub
account. Other personal emails use anonymous example addresses; the GitHub web
committer's public noreply identity is retained.

The history was rewritten to remove private files and identifiers from every
revision, so original hashes and cryptographic signatures cannot be preserved.
Hosted PR descriptions, discussions, review approvals, checks, and attachments
are not stored in Git and are not included here. This document records merge
history rather than claiming those hosted records have been migrated.
