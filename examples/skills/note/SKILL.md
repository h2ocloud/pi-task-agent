---
name: note
description: Save a short note as a markdown file when the user asks to write something down.
triggers: 筆記, 記下來, note
always: false
---

# note

Use this only when the user asks to save a note.

- If they name a path, write that path.
- If they do not, write `./note.md` in the working directory.
- Do not invent other paths, and do not say the file exists until the write tool succeeds.
