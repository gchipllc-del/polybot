# DISARM — stop all live trading, fast

Real money has been traded from this repo (see `docs/FINDINGS.md` → LIVE-MONEY SAFETY).
This is the one-page procedure to guarantee no live order can fire, and how to verify it.

## The gotcha: there are two clones
- The **paper sleeves** (asos / sports / weatherfade=fc2s+ensemble+shadow / seriescollect)
  run from the **backtest clone** (`~/polybot-backtest`).
- The **live agents** (`kalshi_daily*`, `kalshi_hermes`, `weather*`, `trade`, `harvester`,
  `monitor`) run from `~/Desktop/projects/polybot`. They read **that** clone's
  `config/settings.yaml` and `.env` — editing the backtest clone does NOT affect them.

macOS TCC blocks an automated/agent shell from touching `~/Desktop`. Use a real
**Terminal.app with Full Disk Access** for anything under the Desktop clone.

## Three independent gates (all must fail for a live order)
1. A live-path **launchd agent** is loaded AND on disk (reloads at login).
2. `config/settings.yaml → kalshi_daily_live.enabled: true`.
3. `data/kalshi_live_smoke_passed.marker` exists AND kill-switch untripped
   (`kalshi_live_executor.is_live_enabled()`).

Disarm = break as many as possible. For real money, break all three.

## Disarm sequence
```bash
# 1) Unload the running live agents (immediate; reversible with `launchctl load`)
for a in kalshi_daily kalshi_daily_conservative kalshi_daily_hermes kalshi_hermes \
         weather weather_hermes weather_daily trade harvester monitor; do
  launchctl unload "$HOME/Library/LaunchAgents/com.jesse.polybot.$a.plist" 2>/dev/null
done

# 2) Remove the plist files so they do NOT reload at next login (re-installable later)
for a in kalshi_daily kalshi_daily_conservative kalshi_daily_hermes kalshi_hermes \
         weather weather_hermes weather_daily trade harvester monitor; do
  rm -f "$HOME/Library/LaunchAgents/com.jesse.polybot.$a.plist"
done

# 3) Flip the master switch in the LIVE clone — run in your own Terminal (Full Disk Access)
sed -i '' 's/^\( *enabled:\) true/\1 false/' ~/Desktop/projects/polybot/config/settings.yaml
# 3b) belt: drop the smoke marker if present
rm -f ~/Desktop/projects/polybot/data/kalshi_live_smoke_passed.marker
```

## Verify
```bash
launchctl list | grep polybot          # no kalshi_daily*/kalshi_hermes/weather*/trade/harvester/monitor
cd ~/Desktop/projects/polybot && python scripts/preflight_live_check.py; echo "exit=$?"
# expect: "no open real exposure" + "LIVE SWITCH: disarmed" + exit 0
```
If the preflight reports **OPEN LIVE EXPOSURE**, there is a real position open — handle it
before walking away.

## Standing guard (do once, on the LIVE clone)
```bash
cd ~/Desktop/projects/polybot && bash scripts/launchd/install_preflight_agent.sh
```
Runs `preflight_live_check.py` hourly; alarms to `logs/preflight.log` (+ Telegram if
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are in `.env`) on any open exposure or armed switch.

## To intentionally re-arm later (deliberate, not by accident)
Re-create the agents (`install_*_agents.sh`), set `enabled: true`, run the smoke test to
regenerate the marker. All three gates must be set on purpose — that is the point.
