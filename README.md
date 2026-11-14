# ERA V5 Lab tracker

One HTML page that shows how every team in the ERA V5 lab is moving: a status for each team, weekly
activity, PRs with links, planned tasks, who depends on whom, a runbook for building a small model with
agents, and short learning notes.

The page is built on your own machine from three things: your clone of
[theschoolofai/era-v5-lab](https://github.com/theschoolofai/era-v5-lab), the lab's PRs on GitHub, and two
YAML files in this repo. Nothing is uploaded anywhere. The generated page holds GitHub logins, so it stays
in `out/` and is never committed.

## Get access

This repo is private. Ask the owner to add your GitHub account as a collaborator
(**Settings → Collaborators → Add people**), then accept the invite from your email or
https://github.com/notifications.

## Set up (once)

You need git, Python 3.8 or newer, and a clone of the lab repo. Put the two folders side by side:

```bash
cd ~/workspace                                    # any folder
git clone https://github.com/theschoolofai/era-v5-lab.git     # skip if you already have it
git clone https://github.com/sureshmantha/era-v5-lab-tracker.git
cd era-v5-lab-tracker
python3 -m pip install -r requirements.txt        # PyYAML only
```

Your lab clone can be a fork with any remote names: the tracker fetches the lab by its URL into its own refs
(`refs/remotes/tracker/*`) and does not touch your branches or remotes.

## Run

```bash
python3 build.py --my-team 07        # use your team number; it is starred on the page
open out/index.html                  # macOS; on Linux: xdg-open out/index.html; on Windows: start out\index.html
```

The first run fetches every PR head, which takes a minute. Later runs take about 15 seconds.

If the lab clone is not next to this folder, tell the tracker where it is:

```bash
python3 build.py --repo /path/to/era-v5-lab
export ERA_LAB_REPO=/path/to/era-v5-lab    # or set it once in your shell profile
```

| Option | What it does |
|---|---|
| `--my-team NN` | Star your team on the page |
| `--repo PATH` | Where your era-v5-lab clone is (default: `$ERA_LAB_REPO`, then the parent folder, then a sibling `era-v5-lab` folder) |
| `--offline` | No network: use the last fetch and the cached API answers in `cache/` |
| `--no-fetch` | Skip `git fetch`, still call the GitHub API |
| `--reviews` | Also read each open PR's review state and last comment. Needs a token (below) |

### GitHub API limit and the optional token

Without a token GitHub allows 60 API calls an hour, and one build uses about 10, so normal use needs no
token. For `--reviews` (one call per open PR) create a read-only fine-grained token at
https://github.com/settings/tokens with access to public repositories only, and pass it in the environment:

```bash
GITHUB_TOKEN=<your token> python3 build.py --reviews
```

The token is read from the environment only and never written to a file. Do not put it in a script you commit.

## What is on the page

| Tab | What it shows |
|---|---|
| Overview | Numbers for the week, the 10 stages from data to release with each team's status, what is working and what needs attention, merges per team per week, a dependency matrix, planned tasks, recent and long-open PRs |
| A page per team | Why its status is what it is, its tasks, PRs and issues, open items and TODOs from its files, its documents, what it receives from and gives to other teams, learning notes |
| Runbook | Each stage of building a small model: what it takes and gives, the gate that proves it, pitfalls, and a prompt to start an agent |
| Learn | How the lab works (one task one PR, contracts first, the 3-day rule, determinism), a glossary, how to read the page |
| All PRs | Every PR, filtered by state, team folder or text |

## Where each piece comes from

| Source | Read with | Gives |
|---|---|---|
| The lab's `main` | `git ls-tree`, `git cat-file` | files, docs and their titles, tests, TSAI job requests, TODO comments, "Open items / Risks / Next steps" sections, mentions of other teams |
| `refs/pull/*/head` | `git fetch` and `git diff base...head` | the files each PR changes; the team of a PR comes from its folders, not its title |
| GitHub REST API | `pulls`, `issues` | titles, authors, states, dates, comment counts, issues |
| `plan.yaml` (edit by hand) | YAML | weeks and gates, stages, contracts between teams, planned tasks with their file paths and due dates |
| `learn.yaml` (edit by hand) | YAML | principles, glossary, runbook with agent prompts, a note for each team |

## How the status is computed

- **needs help (red):** a planned task is past due, or 3 or more PRs are past 3 days, or nothing merged in 7 days while 2 or more PRs are open.
- **watch (amber):** any other warning: a PR past 3 days, a PR with no comment after a day, no merge into the team folder for 4 days.
- **on track (green):** no warning.
- **A task** is done when its file is on main (and contains the `check:` text when one is given), in review when an open PR touches the file, late when the due date has passed. PR titles such as "week 2 task 6" also become tasks.

The rules live in `build.py` (look for `signals per team`).

## Contributing

The page is only as good as `plan.yaml` and `learn.yaml`. Please send changes as PRs to this repo:

1. Add your team's tasks for the week from the lab plan sheet to `plan.yaml`, with the file path each task creates. Use roles (Member A, Lead), not names.
2. Add a week's theme and gate under `weeks:` when the plan sheet has them.
3. Add a learning note to `learn.yaml` when you learn something worth sharing.
4. End your team's documents in the lab repo with an `## Open items` section; it shows up on your team page.

Do not commit `out/` or `cache/`: they hold GitHub logins of lab members (both are in `.gitignore`).

## Rebuild every morning (optional)

macOS or Linux, with cron (`crontab -e`), adjusting the paths:

```
0 7 * * * cd $HOME/workspace/era-v5-lab-tracker && /usr/bin/python3 build.py --my-team 07 >> out/build.log 2>&1
```

On macOS a launchd agent also works, and runs a missed job when the Mac wakes.

## Files

| File | Purpose |
|---|---|
| `build.py` | Collects the data, computes the signals, writes `out/index.html` and `out/data.json` |
| `template.html` | The page (plain HTML, CSS and JavaScript, no libraries); the data is embedded at build time |
| `plan.yaml` | What the lab plans |
| `learn.yaml` | Learning notes and the runbook |
