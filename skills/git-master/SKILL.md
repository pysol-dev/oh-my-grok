---
name: git-master
description: Commit discipline for oh-my-grok executor work. Written for this repository; it is guidance delivered to the executor, and the engine checks only the structural consequences listed at the end.
---

# git-master (oh-my-grok executor)

You are changing code in a job worktree on a job branch. Leave a history a reviewer can read and a gate can check.

## Rules

1. Work only on the job branch named in your task packet. Never switch to, commit on, merge into, or push the base
   branch, and never push anywhere. Integration is not your job.
2. Make small, coherent commits: one logical change each (a fix, a test, a refactor), not a single dump of everything.
   A reviewer should be able to read each commit on its own. Stay at or below the commit budget in the packet.
3. Keep history linear. Do not create merge commits. If you need a newer base, stop and report it as blocked;
   do not merge or rebase onto something the plan did not name.
4. Every commit message ends with a trailer line giving the job id, exactly as the packet's `commit_trailer` field
   says, for example `Omg-Job: <job id>`. The first line is an imperative summary of at most 72 characters; the body
   (optional) explains why.
5. Never rewrite history that a previous attempt already produced: no `reset --hard` of earlier commits, no
   `commit --amend` of a commit that was already reported as a candidate, no force pushes, no deleting branches.
   A repair attempt adds new commits on top of the rejected candidate.
6. Do not commit secrets, credentials, `.env` files, build output, caches, or the engine's `.omo/` directory.
7. Do not weaken tooling to make a check pass: no new lint or type suppression comments, no edits to checker
   configuration files, no skipped or deleted tests. The language-server gate reads these from the base revision and
   fails the candidate if you try.
8. Before reporting a candidate, check `git status` is clean and `git log --oneline <base>..HEAD` shows only your
   commits, then report the final commit sha in your result.

## What the engine can and cannot check

When `git_discipline` is `enforce`, the final gate verifies, from git itself and not from what you say: the history
from the base to the candidate has no merge commits, has at most the configured number of commits, every commit
message carries the job trailer, and the candidate descends from every earlier candidate of this job. A repair that
replaces that history (`reset --hard` back to the base, or amending a commit already reported) is refused at submit
with `git_history_not_descendant` and fails the same check in the final gate. The engine still cannot tell whether
you read this document or whether your commits are well scoped; independent code review judges that.
