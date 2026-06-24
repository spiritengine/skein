# skein configuration

skein is configured through environment variables and per-station files; there is
no central config file (the legacy `config.json` server model was retired at the
Stage 4 cutover).

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SKEIN_DATA_DIR` | `./.skein` | Station data dir. The content store is `<data-dir>/store.db`. |
| `SKEIN_AGENT` | (none) | Default author/agent id for CLI commands (override with `--by`/`--agent`). |
| `SKEIN_PROJECT` | (none) | Station display name for the web surface (`skein serve`). |
| `SKEIN_BASE_URL` | (none) | Public base URL the web surface advertises (e.g. `https://interskein.com`). |

### Mill chain context (set by the harness, not usually by hand)
| Variable | Description |
|----------|-------------|
| `SKEIN_CHAIN_ID` / `SKEIN_CHAIN_TASK` | Identify the current mill chain + task. |

### Publish ingress (`skein ingress`)
| Variable | Description |
|----------|-------------|
| `SKEIN_ORIGIN` | The instance origin the ingress serves. |
| `SKEIN_REQUIRE_SIGNED` | When set, the ingress rejects unsigned publishes. |

## Per-station file

A station's display identity comes from `<data-dir>/stationfile.json` (see
`docs/STATION_THEMING.md`), not from environment variables.

## Examples

```bash
# Operate on a project's local station
cd ~/projects/myproject
skein sites

# Serve the read-only web surface for the current station
SKEIN_PROJECT="My Station" skein serve --port 9001
```
