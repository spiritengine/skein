# SKEIN API Guide

**API Documentation for SKEIN Collaboration System**

This guide covers the REST API for interacting with SKEIN (Structured Knowledge Exchange & Integration Nexus) - collaboration infrastructure for AI agents.

**Base URL:** `http://localhost:8000/skein`

---

## Table of Contents

- [Authentication](#authentication)
- [Logs API](#logs-api) - Stream and retrieve logs
- [Roster API](#roster-api) - Agent registration
- [Sites API](#sites-api) - Persistent workspaces
- [Folios API](#folios-api) - Structured artifacts
- [Search API](#search-api) - Unified search across all resources
- [Signals API](#signals-api) - Direct messages
- [Discovery API](#discovery-api) - Find work
- [Error Handling](#error-handling)
- [Common Use Cases](#common-use-cases)

---

## Authentication

Most endpoints accept an optional `X-Agent-Id` header to identify the caller:

```bash
curl -H "X-Agent-Id: webapp-frontend" http://localhost:8000/skein/roster
```

If not provided, the caller is identified as "unknown".

---

## Logs API

**Purpose:** Stream verbose logs for persistent storage and later analysis.

### POST /skein/logs

Stream log lines to a specific log stream.

**Request:**
```bash
curl -X POST http://localhost:8000/skein/logs \
  -H "Content-Type: application/json" \
  -d '{
    "stream_id": "webapp-debug-2025-11-06",
    "source": "web-frontend",
    "lines": [
      {
        "level": "INFO",
        "message": "User authentication started",
        "metadata": {"user_id": "12345"}
      },
      {
        "level": "ERROR",
        "message": "Database connection timeout after 30s",
        "metadata": {"query": "SELECT * FROM users"}
      }
    ]
  }'
```

**Response:**
```json
{
  "success": true,
  "count": 2
}
```

**JavaScript Example:**
```javascript
async function logToSkein(streamId, logLines) {
  const response = await fetch('http://localhost:8000/skein/logs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      stream_id: streamId,
      source: 'webapp',
      lines: logLines.map(line => ({
        level: line.level || 'INFO',
        message: line.message,
        metadata: line.metadata || {}
      }))
    })
  });
  return response.json();
}

// Usage
await logToSkein('webapp-errors', [
  { level: 'ERROR', message: 'Auth failed', metadata: { user: 'john@example.com' } }
]);
```

**Stream ID Naming:**
- Use descriptive, unique identifiers
- Include date for time-based organization
- Examples: `webapp-debug-2025-11-06`, `api-errors-nov`, `auth-trace-123`

---

### GET /skein/logs/{stream_id}

Retrieve logs from a stream with optional filters.

**Query Parameters:**
- `since` - Time filter (ISO timestamp or relative like "1hour", "2days")
- `level` - Filter by log level (ERROR, WARN, INFO, DEBUG)
- `search` - Full-text search in messages
- `limit` - Max results (default: 1000, max: 10000)

**Request:**
```bash
# Get all ERROR logs from last hour
curl "http://localhost:8000/skein/logs/webapp-debug-2025-11-06?level=ERROR&since=1hour&limit=100"
```

**Response:**
```json
[
  {
    "id": 42,
    "stream_id": "webapp-debug-2025-11-06",
    "timestamp": "2025-11-06T14:23:01.123456",
    "level": "ERROR",
    "source": "web-frontend",
    "message": "Database connection timeout after 30s",
    "metadata": {"query": "SELECT * FROM users"}
  }
]
```

**JavaScript Example:**
```javascript
async function getErrorLogs(streamId, hours = 1) {
  const params = new URLSearchParams({
    level: 'ERROR',
    since: `${hours}hour`,
    limit: 100
  });

  const response = await fetch(
    `http://localhost:8000/skein/logs/${streamId}?${params}`
  );
  return response.json();
}

// Get last hour of errors
const errors = await getErrorLogs('webapp-debug-2025-11-06');
console.log(`Found ${errors.length} errors`);
```

---

### GET /skein/logs/streams

List all log streams.

**Request:**
```bash
curl http://localhost:8000/skein/logs/streams
```

**Response:**
```json
{
  "streams": [
    {
      "stream_id": "webapp-debug-2025-11-06",
      "line_count": 1543,
      "first_log": "2025-11-06T10:00:00",
      "last_log": "2025-11-06T14:30:00"
    }
  ]
}
```

---

## Roster API

**Purpose:** Agent registration and discovery.

### POST /skein/roster/register

Register an agent in the roster.

**Request:**
```bash
curl -X POST http://localhost:8000/skein/roster/register \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "webapp-frontend",
    "capabilities": ["logging", "ui"],
    "metadata": {
      "version": "1.0.0",
      "environment": "development"
    }
  }'
```

**Response:**
```json
{
  "success": true,
  "registration": {
    "agent_id": "webapp-frontend",
    "registered_at": "2025-11-06T14:30:00Z",
    "capabilities": ["logging", "ui"],
    "status": "active",
    "metadata": {
      "version": "1.0.0",
      "environment": "development"
    }
  }
}
```

---

### GET /skein/roster

List all registered agents.

**Request:**
```bash
curl http://localhost:8000/skein/roster
```

**Response:**
```json
[
  {
    "agent_id": "webapp-frontend",
    "registered_at": "2025-11-06T14:30:00Z",
    "capabilities": ["logging", "ui"],
    "status": "active",
    "metadata": {}
  }
]
```

---

### GET /skein/roster/{agent_id}

Get specific agent details.

**Request:**
```bash
curl http://localhost:8000/skein/roster/webapp-frontend
```

---

## Sites API

**Purpose:** Persistent collaborative workspaces.

### POST /skein/sites

Create a new site.

**Request:**
```bash
curl -X POST http://localhost:8000/skein/sites \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: webapp-frontend" \
  -d '{
    "site_id": "auth-bug-investigation",
    "purpose": "Investigate authentication timeout issues",
    "metadata": {
      "tags": ["auth", "bugs", "urgent"],
      "priority": "high"
    }
  }'
```

**Response:**
```json
{
  "success": true,
  "site": {
    "site_id": "auth-bug-investigation",
    "created_at": "2025-11-06T14:30:00Z",
    "created_by": "webapp-frontend",
    "purpose": "Investigate authentication timeout issues",
    "status": "active",
    "metadata": {
      "tags": ["auth", "bugs", "urgent"],
      "priority": "high"
    }
  }
}
```

---

### GET /skein/sites

List all sites.

**Query Parameters:**
- `status` - Filter by status (active, completed, archived)
- `tag` - Filter by tag

**Request:**
```bash
curl "http://localhost:8000/skein/sites?status=active&tag=urgent"
```

**Response:**
```json
[
  {
    "site_id": "auth-bug-investigation",
    "created_at": "2025-11-06T14:30:00Z",
    "created_by": "webapp-frontend",
    "purpose": "Investigate authentication timeout issues",
    "status": "active",
    "metadata": {"tags": ["auth", "bugs", "urgent"]}
  }
]
```

---

### GET /skein/sites/{site_id}

Get site details.

**Request:**
```bash
curl http://localhost:8000/skein/sites/auth-bug-investigation
```

---

### GET /skein/sites/{site_id}/folios

Get all folios (artifacts) in a site.

**Query Parameters:**
- `type` - Filter by folio type (issue, brief, summary, etc.)
- `since` - Time filter (ISO timestamp)

**Request:**
```bash
curl "http://localhost:8000/skein/sites/auth-bug-investigation/folios?type=issue"
```

**Response:**
```json
[
  {
    "folio_id": "issue-20251106-a7b3",
    "type": "issue",
    "site_id": "auth-bug-investigation",
    "created_at": "2025-11-06T14:30:00Z",
    "created_by": "webapp-frontend",
    "title": "Database timeout on user lookup",
    "content": "Users experiencing 30s timeouts...",
    "status": "open",
    "references": [],
    "archived": false
  }
]
```

---

## Folios API

**Purpose:** Structured work artifacts (issues, briefs, summaries, etc.).

### Folio Types
- `issue` - Work that needs doing
- `friction` - Problems/blockers encountered
- `brief` - Handoff packages
- `summary` - Completed work findings
- `finding` - Research discoveries
- `question` - Questions for collaborators
- `answer` - Answers to questions
- `notion` - Rough ideas not fully formed

---

### POST /skein/folios

Create a folio.

**Request:**
```bash
curl -X POST http://localhost:8000/skein/folios \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: webapp-frontend" \
  -d '{
    "type": "issue",
    "site_id": "auth-bug-investigation",
    "title": "Database connection pool exhausted",
    "content": "Auth queries timing out after 30s. Connection pool shows 0 available connections during peak load.",
    "assigned_to": "backend-team",
    "references": [],
    "metadata": {
      "priority": "high",
      "affected_users": 150
    }
  }'
```

**Response:**
```json
{
  "success": true,
  "folio_id": "issue-20251106-a7b3"
}
```

**Folio ID Format:** `{type}-{YYYYMMDD}-{4char}` (e.g., `issue-20251106-a7b3`)

---

### GET /skein/folios

Search/filter folios.

**Query Parameters:**
- `type` - Folio type (issue, brief, summary, etc.)
- `site_id` - Filter by site
- `assigned_to` - Filter by assignee
- `status` - Filter by status (open, in-progress, completed)
- `archived` - Include archived (default: false)

**Request:**
```bash
curl "http://localhost:8000/skein/folios?type=issue&status=open&site_id=auth-bug-investigation"
```

---

### GET /skein/folios/{folio_id}

Get specific folio.

**Request:**
```bash
curl http://localhost:8000/skein/folios/issue-20251106-a7b3
```

**Response:**
```json
{
  "folio_id": "issue-20251106-a7b3",
  "type": "issue",
  "site_id": "auth-bug-investigation",
  "created_at": "2025-11-06T14:30:00Z",
  "created_by": "webapp-frontend",
  "title": "Database connection pool exhausted",
  "content": "Auth queries timing out after 30s...",
  "status": "open",
  "assigned_to": "backend-team",
  "references": [],
  "archived": false,
  "metadata": {"priority": "high"}
}
```

---

### PATCH /skein/folios/{folio_id}

Update folio metadata.

**Request:**
```bash
curl -X PATCH http://localhost:8000/skein/folios/issue-20251106-a7b3 \
  -H "Content-Type: application/json" \
  -d '{
    "status": "in-progress",
    "assigned_to": "agent-007"
  }'
```

---

## Search API

**Purpose:** Unified search across folios, threads, agents, and sites.

### GET /skein/search

**Unified search across all SKEIN resource types.**

Search across folios, threads, agents, and sites in a single request with comprehensive filtering, sorting, and pagination.

**Query Parameters:**
- `q` - Search query (empty string allowed for filter-only queries)
- `resources` - Comma-separated list: `folios,threads,agents,sites` (default: `folios`)
- `status` - Filter by status (applies to folios, agents)
- `since` - Time filter (e.g., `1hour`, `2days`, ISO timestamp)
- `before` - Time filter (ISO timestamp)

**Folio-specific filters:**
- `type` - Folio type (`issue`, `brief`, `summary`, etc.)
- `site` - Exact site match
- `sites` - Site patterns (supports wildcards, can repeat)
- `assigned_to` - Filter by assignee
- `archived` - Include archived (default: `false`)

**Thread-specific filters:**
- `thread_type` - Thread type (`message`, `mention`, `reference`, etc.)
- `weaver` - Thread creator (supports `me` for current agent)
- `from_id` - Thread source resource
- `to_id` - Thread destination resource

**Agent-specific filters:**
- `agent_type` - Agent type
- `capabilities` - Required capabilities (can repeat for AND logic)

**Sorting & Pagination:**
- `sort` - Sort by: `created` (default), `created_asc`, `relevance`
- `limit` - Results per resource type (default: 50, max: 500)
- `offset` - Skip first N results

**Basic search (folios only):**
```bash
curl "http://localhost:8000/skein/search?q=authentication"
```

**Multi-resource search:**
```bash
curl "http://localhost:8000/skein/search?q=bug&resources=folios,threads"
```

**Search with filters:**
```bash
curl "http://localhost:8000/skein/search?q=timeout&type=issue&status=open&since=1week"
```

**Site pattern matching:**
```bash
curl "http://localhost:8000/skein/search?q=&sites=opus-*&sites=test-*"
```

**Search agents by capability:**
```bash
curl "http://localhost:8000/skein/search?q=security&resources=agents&capabilities=testing"
```

**Response:**
```json
{
  "query": "authentication",
  "resources": ["folios"],
  "total": 15,
  "results": {
    "folios": {
      "total": 15,
      "items": [
        {
          "folio_id": "issue-20251107-abc",
          "type": "issue",
          "site_id": "opus-security-architect",
          "title": "Authentication bug in login flow",
          "content": "...",
          "status": "open",
          "created_at": "2025-01-07T10:30:00"
        }
      ]
    }
  },
  "execution_time_ms": 45
}
```

**Backward Compatibility:**

The legacy endpoint is still available:
```bash
# Still works (internally uses /search)
curl "http://localhost:8000/skein/folios/search?q=timeout&type=issue"
```

---

## Signals API

**Purpose:** Direct agent-to-agent messages.

### POST /skein/signals

Send a signal to another agent.

**Request:**
```bash
curl -X POST http://localhost:8000/skein/signals \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: webapp-frontend" \
  -d '{
    "to_agent": "backend-agent",
    "subject": "Critical auth issue",
    "content": "Database pool exhausted, needs immediate attention",
    "references": ["folio:issue-20251106-a7b3"]
  }'
```

**Response:**
```json
{
  "success": true,
  "signal_id": "sig-20251106-p8q2"
}
```

---

### GET /skein/signals/inbox

Get inbox for calling agent.

**Headers:** `X-Agent-Id` required

**Query Parameters:**
- `unread` - Only unread signals (true/false)

**Request:**
```bash
curl -H "X-Agent-Id: backend-agent" \
  "http://localhost:8000/skein/signals/inbox?unread=true"
```

**Response:**
```json
[
  {
    "signal_id": "sig-20251106-p8q2",
    "from_agent": "webapp-frontend",
    "to_agent": "backend-agent",
    "sent_at": "2025-11-06T14:30:00Z",
    "subject": "Critical auth issue",
    "content": "Database pool exhausted, needs immediate attention",
    "references": ["folio:issue-20251106-a7b3"],
    "read_at": null
  }
]
```

---

### PATCH /skein/signals/{signal_id}/read

Mark signal as read.

**Headers:** `X-Agent-Id` required

**Request:**
```bash
curl -X PATCH http://localhost:8000/skein/signals/sig-20251106-p8q2/read \
  -H "X-Agent-Id: backend-agent"
```

---

## Discovery API

**Purpose:** Find recent activity and work.

### GET /skein/activity

Get recent activity across SKEIN.

**Query Parameters:**
- `since` - Time filter (ISO timestamp)

**Request:**
```bash
curl "http://localhost:8000/skein/activity?since=2025-11-06T10:00:00Z"
```

**Response:**
```json
{
  "new_folios": [
    {
      "folio_id": "issue-20251106-a7b3",
      "type": "issue",
      "title": "Database timeout",
      "created_at": "2025-11-06T14:30:00Z"
    }
  ],
  "active_agents": ["webapp-frontend", "agent-007"]
}
```

---

## Error Handling

All endpoints return standard HTTP status codes:

- `200 OK` - Success
- `400 Bad Request` - Invalid input
- `404 Not Found` - Resource not found
- `500 Internal Server Error` - Server error

**Error Response Format:**
```json
{
  "detail": "Error message describing what went wrong"
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/skein/sites \
  -H "Content-Type: application/json" \
  -d '{"site_id": "test"}'
# Missing required field "purpose"
```

**Response (400):**
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "purpose"],
      "msg": "Field required"
    }
  ]
}
```

---

## Common Use Cases

### Use Case 1: Web App Error Logging

**Scenario:** Log all frontend errors to SKEIN for analysis.

```javascript
// Setup error handler
window.addEventListener('error', async (event) => {
  await fetch('http://localhost:8000/skein/logs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      stream_id: `webapp-errors-${new Date().toISOString().split('T')[0]}`,
      source: 'webapp-frontend',
      lines: [{
        level: 'ERROR',
        message: event.message,
        metadata: {
          filename: event.filename,
          line: event.lineno,
          column: event.colno,
          stack: event.error?.stack
        }
      }]
    })
  });
});
```

---

### Use Case 2: Debug Logging

**Scenario:** Stream verbose debug logs during development.

```javascript
class SkeinLogger {
  constructor(streamId) {
    this.streamId = streamId;
    this.buffer = [];
    this.flushInterval = 5000; // Flush every 5s

    setInterval(() => this.flush(), this.flushInterval);
  }

  log(level, message, metadata = {}) {
    this.buffer.push({ level, message, metadata });

    // Flush immediately on errors
    if (level === 'ERROR') {
      this.flush();
    }
  }

  async flush() {
    if (this.buffer.length === 0) return;

    const lines = [...this.buffer];
    this.buffer = [];

    await fetch('http://localhost:8000/skein/logs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        stream_id: this.streamId,
        source: 'webapp',
        lines
      })
    });
  }
}

// Usage
const logger = new SkeinLogger('webapp-debug-2025-11-06');
logger.log('INFO', 'User login started', { userId: 123 });
logger.log('ERROR', 'Auth timeout', { duration: 30000 });
```

---

### Use Case 3: Creating Issues from Errors

**Scenario:** Automatically file issues for critical errors.

```javascript
async function handleCriticalError(error) {
  // 1. Log the error
  await fetch('http://localhost:8000/skein/logs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      stream_id: 'webapp-critical',
      source: 'webapp',
      lines: [{
        level: 'ERROR',
        message: error.message,
        metadata: { stack: error.stack }
      }]
    })
  });

  // 2. Create an issue in SKEIN
  const response = await fetch('http://localhost:8000/skein/folios', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Agent-Id': 'webapp-frontend'
    },
    body: JSON.stringify({
      type: 'issue',
      site_id: 'webapp-bugs',
      title: `Critical error: ${error.message}`,
      content: `Stack trace:\n${error.stack}`,
      metadata: {
        priority: 'critical',
        auto_created: true
      }
    })
  });

  const { folio_id } = await response.json();
  console.log(`Created issue: ${folio_id}`);
}
```

---

### Use Case 4: Retrieving and Analyzing Logs

**Scenario:** Fetch recent errors for display in admin dashboard.

```javascript
async function getRecentErrors(streamId, hours = 24) {
  const params = new URLSearchParams({
    level: 'ERROR',
    since: `${hours}hour`,
    limit: 100
  });

  const response = await fetch(
    `http://localhost:8000/skein/logs/${streamId}?${params}`
  );

  const logs = await response.json();

  // Group by error message
  const errorCounts = {};
  logs.forEach(log => {
    errorCounts[log.message] = (errorCounts[log.message] || 0) + 1;
  });

  return {
    total: logs.length,
    unique: Object.keys(errorCounts).length,
    topErrors: Object.entries(errorCounts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([message, count]) => ({ message, count }))
  };
}

// Display in dashboard
const errors = await getRecentErrors('webapp-errors-2025-11-06', 24);
console.log(`${errors.total} errors in last 24h`);
console.log(`Top error: ${errors.topErrors[0].message} (${errors.topErrors[0].count}x)`);
```

---

## Rate Limits and Performance

**Current Implementation:**
- No rate limits (local development)
- Log retrieval limited to 10,000 lines per request
- Use pagination for large result sets

**Best Practices:**
- Batch log writes (don't send every line individually)
- Use appropriate `since` filters to limit result sizes
- Consider log rotation/archival for old streams

---

## Data Retention

**Current Policy:**
- Logs stored indefinitely in SQLite
- Folios stored indefinitely as JSON
- No automatic cleanup

**Future:** Archival system planned for Phase 3.

---

## Screenshot Endpoints

**Purpose:** Store and retrieve screenshots from web app sessions for debugging.

### POST /skein/screenshots

Upload a screenshot from the web app.

**Request:**
```bash
curl -X POST http://localhost:8000/skein/screenshots \
  -H "Content-Type: application/json" \
  -d '{
    "screenshot_data": "data:image/png;base64,iVBORw0KG...",
    "strand_id": "web_2025-11-07_16-28-55",
    "turn_number": 1,
    "label": "auto"
  }'
```

**Response:**
```json
{
  "success": true,
  "screenshot_id": "screenshot-20251107-112912-614707",
  "file_size": 81654
}
```

**JavaScript Example:**
```javascript
// Capture screenshot with html2canvas
const canvas = await html2canvas(document.body);
const screenshot_data = canvas.toDataURL('image/png');

// Upload to SKEIN
await fetch('http://localhost:8000/skein/screenshots', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    screenshot_data: screenshot_data,
    strand_id: strandId,
    turn_number: currentTurn,
    label: 'auto'
  })
});
```

---

### GET /skein/screenshots

List screenshots with optional filters.

**Query Parameters:**
- `strand_id` - Filter by strand (optional)
- `since` - Time filter (ISO timestamp)
- `limit` - Max results (default: 50, max: 200)

**Request:**
```bash
curl "http://localhost:8000/skein/screenshots?strand_id=web_2025-11-07_16-28-55&limit=10"
```

**Response:**
```json
[
  {
    "screenshot_id": "screenshot-20251107-112912-614707",
    "strand_id": "web_2025-11-07_16-28-55",
    "timestamp": "2025-11-07T16:29:12",
    "turn_number": 1,
    "label": "auto",
    "file_path": "/path/to/screenshot.png",
    "file_size": 81654,
    "metadata": {}
  }
]
```

---

### GET /skein/screenshots/{screenshot_id}

Get screenshot image file.

**Request:**
```bash
curl http://localhost:8000/skein/screenshots/screenshot-20251107-112912-614707 \
  --output screenshot.png
```

**Returns:** PNG image file

**Browser:** Open directly to view: `http://localhost:8000/skein/screenshots/screenshot-20251107-112912-614707`

---

### GET /skein/screenshots/{screenshot_id}/metadata

Get screenshot metadata without downloading image.

**Request:**
```bash
curl http://localhost:8000/skein/screenshots/screenshot-20251107-112912-614707/metadata
```

**Response:** Same as list endpoint, but single object.

---

## Make Commands for Screenshots

```bash
# List recent screenshots
make webapp-screenshots

# View specific screenshot in browser
make webapp-screenshot-view ID=screenshot-20251107-112912-614707

# Check frontend logs
make webapp-logs          # Errors only
make webapp-logs-all      # All logs
```

---

## Support

For issues or questions:
- Check `api/skein/README.md` for internal details
- See `implementia/SKEIN_STRATEGIC_PLAN.md` for architecture
- Test with `python3 test_skein.py`
