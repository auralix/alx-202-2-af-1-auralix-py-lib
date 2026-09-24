# AGENTS.md


---

# Global Auralix Rules
- Before starting or resuming a session, locate the wiki using these paths in order, relative to this repository's root, where `*` matches exactly one folder level
	1. `../alx-99-af-1-auralix-wiki`
	2. `../../alx-99-af-1-auralix-wiki`
	3. `../*/alx-99-af-1-auralix-wiki`
	4. `../../*/alx-99-af-1-auralix-wiki`
- Use the first matching priority and ask the user for the location if none match or that priority has multiple matches
- Confirm the wiki is on `master`, run `git pull --ff-only origin master` there, then read and follow its root `AGENTS.md`
- Do not switch branches, stash, reset or discard local changes to perform the update
- If the location is unresolved, the branch is not `master`, the pull fails or the rules cannot be read, report the problem and stop before modifying task files
- Keep this repository as the working repository


---

# Rules for This Repository
- No additional rules
