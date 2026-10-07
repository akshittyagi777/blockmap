---
name: blockmap
description: Build an interactive code map of a git repo/branch (data flow between scripts, run order, block-by-block diagram of every file) using blockmap for the structure and subagents for the written descriptions. Use when the user asks to explain, map, diagram or onboard onto a codebase or a specific branch.
---

# blockmap: map a repo with agents instead of an API key

`blockmap` computes the structure (blocks, line ranges, calls, imports, data paths, run order) from the code.
You and your subagents write the words: cheap, parallel readers label blocks; the strongest available model
reviews them and writes the overview. Nothing is committed to the user's repo; work happens in a scratch dir.

## 0. Inputs
- Repo: a local path or git URL. Ref: branch, tag or commit (ask only if the user named none and HEAD is ambiguous).
- Work dir: a scratch directory, e.g. `<scratchpad>/blockmap-<repo>-<ref>`.
- Install once if `blockmap` is missing: `uv tool install git+https://github.com/akshittyagi777/blockmap`.

## 1. Structure (no model)
```bash
blockmap analyze <repo> --ref <ref> <work>
blockmap tasks <work>
```
`<work>/tasks/index.json` lists describe batches; each `describe_NN/TASK.md` holds the instructions, the JSON
schema, and every file of the batch with numbered source.

## 2. Describe (cheap model, parallel)
Spawn one subagent per batch, all in one message so they run concurrently. Use the cheapest capable model
(e.g. Haiku). If the model the user asked for is not available as a subagent option, ask which to use.
Prompt per subagent:
> Read `<work>/tasks/describe_NN/TASK.md` and follow it exactly. For each file section, write one JSON
> file named `<answer name>.json` into `<work>/tasks/describe_NN/`. Each file must be valid JSON matching
> the schema in TASK.md; keep the given block ids and line ranges. Reply "done" when every file is written.

## 3. Verify (strongest model)
Spawn reviewers on the strongest model (one per batch, or batches grouped) with:
> Read `<work>/tasks/VERIFY.md`. For every `<name>.json` answer in `<work>/tasks/describe_NN/`, open the
> source file it describes (under `<work>/tree/`) and write `<name>.verify.json` next to it, matching the schema.

Check a few answers yourself against the code; a reviewer that rubber-stamps is worse than none.

## 4. Synthesize (you, or the strongest model)
```bash
blockmap synth-input <work> > <work>/tasks/synth_input.json
```
Read `<work>/tasks/SYNTH.md` and `synth_input.json`, then write `<work>/tasks/synth.json` (groups, statuses,
overview, findings). Ground every finding in the facts given or in code you read.

## 5. Apply and render
```bash
blockmap apply <work>
blockmap render <work> -o <work>/map.html
```
`apply` validates every answer (unknown block ids, phases that do not tile a block, unknown files) and prints
what it dropped. Fix and re-run if it drops much.

## 6. Deliver
Give the user the HTML file (it is self-contained). If the session can publish artifacts, offer to publish it.
Tell the user which models wrote and reviewed the text, and that structure is computed from the code.
