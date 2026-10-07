# blockmap

Point it at any git repo and branch; get one self-contained HTML page that shows:

- **Data flow.** Which scripts write which files, and which scripts read them, laid out as a graph you can hover and click.
- **Run order.** The sequence your shell scripts call things in, with conditional steps marked.
- **Every file, block by block.** Each Python file drawn as its blocks in order (imports, each function or class, the main guard), with exact line ranges, arrows for calls between functions in the file, and the repo functions each block uses.
- **Who uses what.** Script × package import matrix, plus "used by" links on every module.
- **Optional written layer.** Plain-English labels for every block, a reviewed purpose for every file, groups, an overview, and a short list of things to know before changing anything.

It was built to answer "what does this research repo actually do, and in what order?" on a branch you have not looked at in a month.

## Quick start

```bash
uv tool install git+https://github.com/akshittyagi777/blockmap
```

```bash
blockmap build ~/code/my-repo --ref main --open
```

That run uses no model and costs nothing: blocks, line ranges, calls, imports, data paths and run order all come from parsing the code. Labels come from function names and docstrings.

To add the written layer through the Claude API (needs `ANTHROPIC_API_KEY` or an `ant auth login` profile):

```bash
uv tool install 'blockmap[llm] @ git+https://github.com/akshittyagi777/blockmap'
```

```bash
blockmap build ~/code/my-repo --ref my-branch --llm api --open
```

Any branch, tag or commit works, and so do URLs (`blockmap build https://github.com/org/repo --ref v2.1`). Your checkout is never touched: local repos are read with `git archive`; `--worktree` maps uncommitted files instead.

## How it works

```
snapshot ──> analyze ──────────────────────────> enrich (optional) ──> render
git archive   per file: AST blocks, calls,        worker model: label      one HTML file,
of the ref    imports, read/write paths           every block              map.json inside
              per repo: artifact graph,           judge model: review
              shell run order, import graph       each file + overview
```

**What is computed, and what is written.** Everything structural (block boundaries, line ranges, call arrows, imports, which file writes or reads which path, run order) is computed from the code and is exact up to the limits below. Descriptions, purposes, groups, statuses and findings are written by a model. The page footer says which. In API mode a cheaper worker model (default `claude-haiku-4-5`) labels each file. Then the judge model (default `claude-opus-5-5`) checks each label against the source and writes the overview. That review step is what catches confident-but-wrong descriptions. Skip it with `--no-verify` if you only want a quick pass.

**Data paths.** blockmap reads paths from `open()`, `h5py.File`, `np.save`/`load`, pandas `read_*`/`to_*`, `savefig`, `Path.glob`, `shutil.copy`, Hugging Face `load_dataset`/`snapshot_download`, and from argparse defaults. It follows paths one level into helper functions (`_save(path)` → `h5py.File(path, "w")`). It resolves f-strings and `Path / "x"` into templates like `results/{model}/x.h5`. Readers and writers whose templates match become one node. Directories and globs fan in rather than merge.

## Three ways to run it

| Mode | Command | Text quality | Cost |
|---|---|---|---|
| Static | `blockmap build REPO` | names + docstrings | free |
| API | `blockmap build REPO --llm api` | worker labels, judge-reviewed | your API usage |
| Claude Code skill | `/blockmap` in Claude Code | subagents label, strongest model reviews | your Claude plan |

The API mode caches every answer under `~/.cache/blockmap` keyed by file content, model and prompt version. Re-running on another branch only pays for files that changed. The end of each run prints the token totals.

### Claude Code skill

Copy `skills/blockmap/` into `~/.claude/skills/` (or a project's `.claude/skills/`). In Claude Code, ask it to map a repo or branch. The skill runs `blockmap analyze` and `blockmap tasks` to write prompt files. Parallel subagents answer them. The strongest available model reviews the answers and writes the overview. Then `blockmap apply` validates everything and `blockmap render` writes the page. The step commands also work by hand:

```bash
blockmap analyze REPO --ref main work/
blockmap tasks work/
blockmap synth-input work/
blockmap apply work/
blockmap render work/ -o map.html
```

## Privacy

Static mode sends nothing anywhere. API mode sends each file's source, plus the repo's file list, to the Anthropic API. The Opus judge requests opt into server-side refusal fallback (`fallbacks: "default"`). Do not use API mode on code you are not allowed to share with a third-party service.

## Limits

- Python is analysed in depth. Shell scripts give run order. Other languages are counted and listed but not drawn yet.
- Path detection is static. Paths built from runtime input, config files or environment variables can be missing, and so can I/O inside libraries the repo calls. Every detected path keeps the line it came from.
- Calls are drawn only between top-level functions of the same file. Method-level and cross-file call graphs are not drawn; cross-file use shows up as tags.
- The layout is a simple layered placement. Very large repos (hundreds of scripts) produce a wide graph; use `--include`/`--exclude` globs to focus.

## Development

```bash
uv sync --all-extras
```

```bash
uv run pytest
```

```bash
uv run ruff check src tests
```

The tests use a synthetic repo and a fake API client, so they need no network and no key.

## License

MIT
