# DALI2 Webcam Tourist Planner

> Real-time crowd-aware monument visit planning using DALI2 agents, public webcams, and GPT-4o vision analysis.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Windy Webcam API                      │
│         (public webcam snapshots of monuments)           │
└────────────────────────┬────────────────────────────────┘
                         │  HTTP (every 90s)
                         ▼
┌─────────────────────────────────────────────────────────┐
│              Python Webcam Bridge                        │
│  • Fetches preview images from Windy API                │
│  • Sends images to GPT-4o (OpenRouter) for analysis     │
│  • Publishes crowd_report events to DALI2 via LINDA     │
└────────────────────────┬────────────────────────────────┘
                         │  Redis pub/sub (LINDA channel)
                         ▼
┌─────────────────────────────────────────────────────────┐
│                  DALI2 Agents                            │
│  ┌─────────────┐  ┌──────────┐                          │
│  │   planner   │  │ monitor  │                          │
│  │ • beliefs   │  │ • logs   │                          │
│  │ • planning  │  │ • alerts │                          │
│  │ • AI advice │  │          │                          │
│  └─────────────┘  └──────────┘                          │
│         ↕ Redis star topology                           │
│  ┌──────────────────────────────┐                       │
│  │  Web UI (:8080) + REST API  │                        │
│  └──────────────────────────────┘                       │
└─────────────────────────────────────────────────────────┘
```

**Data flow:**
1. **Webcam Bridge** fetches live snapshots from Windy public webcams
2. Each image is sent to **GPT-4o** (via OpenRouter) which returns crowd level (0-10), weather, and visibility
3. Results are published as `crowd_report` events to the DALI2 **planner** agent via Redis LINDA channel
4. The **planner** agent maintains beliefs about each monument's crowd level and computes an optimal visit route (least crowded first)
5. A **monitor** agent logs all events and provides situational awareness
6. The **Web UI** shows agent beliefs, logs, and allows injecting `plan_visit` or `request_scan` events

## Quick Start (Docker)

```bash
# 1. Edit .env with your API keys
cp .env.example .env
# Set OPENROUTER_API_KEY=sk-or-...

# 2. Start all services
docker compose up --build

# 3. Open the DALI2 Web UI
# http://localhost:8080
```

## Configuration

All settings are in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `WINDY_API_KEY` | Windy webcam API key | (provided) |
| `WEBCAMS` | Comma-separated `id:name:lat:lon` | Rome webcams |
| `OPENROUTER_API_KEY` | OpenRouter API key for GPT-4o | (required) |
| `OPENROUTER_MODEL` | Vision model to use | `openai/gpt-4o` |
| `POLL_INTERVAL` | Seconds between scans (min 60) | `90` |
| `REDIS_HOST` | Redis hostname | `redis` |
| `DALI2_PORT` | Web UI port | `8080` |

### Adding Webcams

Add entries to the `WEBCAMS` variable in `.env`:

```
WEBCAMS=1600351836:Piazza Venezia:41.8962:12.4823,1345830065:Via Torre Argentina:41.8953:12.4766
```

Format: `webcam_id:display_name:latitude:longitude`

Find webcam IDs at [windy.com/webcams](https://www.windy.com/webcams).

## User Interaction

**Via Web UI** (http://localhost:8080):
- View real-time agent logs and beliefs
- Inject `plan_visit` event to the `planner` agent to trigger route computation
- Inject `request_scan` to force an immediate webcam refresh
- View the computed plan in the planner's beliefs (`current_plan`)

**Via REST API:**
```bash
# Trigger visit planning
curl -X POST http://localhost:8080/api/send \
  -H "Content-Type: application/json" \
  -d '{"to":"planner","content":"plan_visit"}'

# Request immediate scan
curl -X POST http://localhost:8080/api/send \
  -H "Content-Type: application/json" \
  -d '{"to":"planner","content":"request_scan"}'

# View planner beliefs (crowd data + plan)
curl http://localhost:8080/api/beliefs?agent=planner
```

## Without Docker

```bash
# Terminal 1: Redis
docker run -d --name dali2-redis -p 6379:6379 redis:7-alpine

# Terminal 2: DALI2
cd ../DALI2
swipl -l src/server.pl -g main -- 8080 ../DALI2-webcam-planner/agents/webcam_planner.pl

# Terminal 3: Webcam Bridge
cd bridge
pip install -r requirements.txt
WINDY_API_KEY=... OPENROUTER_API_KEY=... WEBCAMS=... REDIS_HOST=localhost python webcam_bridge.py
```

## DALI2 Agents

### planner
- Receives `crowd_report` events from the bridge
- Maintains `monument/9` beliefs with real-time crowd/weather data
- Computes optimal visit routes (sorted by crowd level ascending)
- Fires high-crowd alerts when level ≥ 8
- Optionally consults AI oracle for route optimization advice

### monitor
- Logs all system events with timestamps
- Periodic status summaries
- Situational awareness dashboard

## License

Apache License 2.0
