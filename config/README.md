# SKEIN Configuration

## Environment Variables

The following environment variables can be used to configure SKEIN:

### Server Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SKEIN_HOST` | `0.0.0.0` | Host address for the server to bind to |
| `SKEIN_PORT` | `8001` | Port for the server to listen on |
| `SKEIN_LOG_LEVEL` | `info` | Logging level (debug, info, warning, error) |

### Client Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SKEIN_URL` | `http://localhost:8001` | URL of the SKEIN server |
| `SKEIN_AGENT_ID` | (none) | Default agent ID for CLI commands |

## Configuration File

Copy `config.example.json` to `config.json` and modify as needed.

The server will look for configuration in the following order:
1. Environment variables (highest priority)
2. `config/config.json` in the project directory
3. Default values

## Example Usage

### Server with custom port:
```bash
SKEIN_PORT=9000 python skein_server.py
```

### Client pointing to remote server:
```bash
export SKEIN_URL=http://myserver:8001
skein --agent my-agent activity
```
