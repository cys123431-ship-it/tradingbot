# Codex Ops Notes

This file records local operational shortcuts for this workspace. Do not store
private keys, tokens, API secrets, or Telegram credentials here.

## Local Git

`git` is not always available on `PATH` in this Windows workspace. Use the
bundled MinGit executable directly:

```powershell
& 'C:\Users\yosub\Desktop\tradingbot-main\.codex\tools\mingit-2.55.0\cmd\git.exe' status --short
```

Common commit/push flow:

```powershell
$git = 'C:\Users\yosub\Desktop\tradingbot-main\.codex\tools\mingit-2.55.0\cmd\git.exe'
& $git status --short
& $git add -- <changed files only>
& $git commit -m "message"
& $git push origin main
& $git ls-remote origin refs/heads/main
```

Only stage files changed for the current task. Leave unrelated dirty files
untouched.

## Azure SSH

Use the SSH host alias from `%USERPROFILE%\.ssh\config`:

```powershell
ssh -F $env:USERPROFILE\.ssh\config azure-trading-bot
```

Configured server repository:

```bash
/home/azureuser/tradingbot
```

Read-only deployment verification:

```bash
cd /home/azureuser/tradingbot
git rev-parse HEAD
ps -eo pid,etime,args | grep launch_emas | grep -v grep
cat runtime/bot_heartbeat.json
python3 -m py_compile emas.py utbreakout/sizing.py
grep -n "Traceback\|ERROR\|Exception" runtime/utbreakout_diagnostics.log | tail
```

Expected healthy signs:

- `git rev-parse HEAD` matches the pushed GitHub commit.
- `scripts/launch_emas.py` is running.
- `runtime/bot_heartbeat.json` has `"paused": false` and `"engine": "signal"`.
- `py_compile` exits cleanly.
- Recent diagnostics have no `Traceback`, `ERROR`, or `Exception`.

## GitHub Actions

After pushing, confirm the workflow for the new commit:

```powershell
$sha = & $git rev-parse HEAD
$uri = "https://api.github.com/repos/cys123431-ship-it/tradingbot/actions/runs?head_sha=$sha&per_page=5"
Invoke-RestMethod -Uri $uri -Headers @{ 'User-Agent' = 'codex' }
```

The deploy workflow should complete successfully before treating GitHub as
deployed. Azure verification is still required after trading-bot changes.
