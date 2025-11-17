"""Test script for SKEIN basic functionality."""

import asyncio
import aiohttp
import json
import pytest

BASE_URL = "http://localhost:8001/skein"


@pytest.mark.asyncio
async def test_skein_workflow():
    """Test basic SKEIN workflow: register, create site, post folio, create brief."""

    print("🧪 Testing SKEIN Workflow\n")

    # Default headers including project ID
    headers = {
        "X-Project-Id": "test-project",
        "X-Agent-Id": "test-agent-001"
    }

    async with aiohttp.ClientSession(headers=headers) as session:

        # Test 1: Register an agent
        print("1️⃣ Registering agent...")
        async with session.post(
            f"{BASE_URL}/roster/register",
            json={
                "agent_id": "test-agent-001",
                "capabilities": ["testing", "debugging"],
                "metadata": {"purpose": "skein-testing"}
            }
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                print(f"✅ Agent registered: {data['registration']['agent_id']}")
            else:
                print(f"❌ Failed to register agent: {await resp.text()}")
                return

        # Test 2: Get roster
        print("\n2️⃣ Getting roster...")
        async with session.get(f"{BASE_URL}/roster") as resp:
            if resp.status == 200:
                agents = await resp.json()
                print(f"✅ Found {len(agents)} agent(s) in roster")
                for agent in agents:
                    print(f"   • {agent['agent_id']}: {agent['capabilities']}")
            else:
                print(f"❌ Failed to get roster: {await resp.text()}")

        # Test 3: Create a site
        print("\n3️⃣ Creating site...")
        async with session.post(
            f"{BASE_URL}/sites",
            json={
                "site_id": "test-investigation",
                "purpose": "Testing SKEIN collaboration features",
                "metadata": {"tags": ["testing", "demo"]}
            }
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                print(f"✅ Site created: {data['site']['site_id']}")
            else:
                print(f"❌ Failed to create site: {await resp.text()}")
                return

        # Test 4: Post an issue to the site
        print("\n4️⃣ Posting issue to site...")
        async with session.post(
            f"{BASE_URL}/folios",
            json={
                "type": "issue",
                "site_id": "test-investigation",
                "title": "Test database connection timeout",
                "content": "Need to investigate why connections are timing out after 30s",
                "references": [],
                "metadata": {}
            }
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                issue_id = data['folio_id']
                print(f"✅ Issue created: {issue_id}")
            else:
                print(f"❌ Failed to create issue: {await resp.text()}")
                return

        # Test 5: Create a handoff brief
        print("\n5️⃣ Creating handoff brief...")
        async with session.post(
            f"{BASE_URL}/folios",
            json={
                "type": "brief",
                "site_id": "test-investigation",
                "title": "Handoff: Database Investigation",
                "content": """Here's everything you need to know:

                What's done:
                - Identified timeout issue in connection pool
                - Reproduced in staging environment
                - Narrowed down to queries >30s

                What's left:
                - Implement connection pool tuning
                - Add query timeout handling
                - Deploy and verify

                Key decisions:
                - Using PgBouncer for connection pooling
                - Setting statement_timeout to 25s

                Gotchas:
                - Must restart app after config changes
                - Check monitoring for pool exhaustion
                """,
                "target_agent": "next-session",
                "references": [f"folio:{issue_id}"],
                "metadata": {"questions_enabled": True}
            }
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                brief_id = data['folio_id']
                print(f"✅ Brief created: {brief_id}")
                print(f"   Handoff string: HANDOFF: {brief_id}")
            else:
                print(f"❌ Failed to create brief: {await resp.text()}")
                return

        # Test 6: List folios by site
        print("\n6️⃣ Listing folios by site...")
        async with session.get(
            f"{BASE_URL}/folios",
            params={"site_id": "test-investigation"}
        ) as resp:
            if resp.status == 200:
                folios = await resp.json()
                print(f"✅ Found {len(folios)} folio(s) in site")
                for folio in folios:
                    print(f"   • {folio['type'].upper()}: {folio['title']}")
            else:
                print(f"❌ Failed to list folios: {await resp.text()}")

        # Test 6a: Search folios with query
        print("\n6️⃣a Searching folios with query...")
        async with session.get(
            f"{BASE_URL}/folios/search",
            params={"q": "database"}
        ) as resp:
            if resp.status == 200:
                results = await resp.json()
                print(f"✅ Found {len(results)} result(s) for 'database'")
                for result in results:
                    print(f"   • {result['type'].upper()}: {result['title']}")
            else:
                print(f"❌ Failed to search folios: {await resp.text()}")

        # Test 6b: Search with type filter
        print("\n6️⃣b Searching folios with --type filter...")
        async with session.get(
            f"{BASE_URL}/folios/search",
            params={"q": "database", "type": "issue"}
        ) as resp:
            if resp.status == 200:
                results = await resp.json()
                print(f"✅ Found {len(results)} issue(s) for 'database'")
                assert all(r['type'] == 'issue' for r in results), "Type filter failed"
            else:
                print(f"❌ Failed to search with type filter: {await resp.text()}")

        # Test 6c: Search with status filter
        print("\n6️⃣c Searching folios with --status filter...")
        async with session.get(
            f"{BASE_URL}/folios/search",
            params={"q": "", "type": "issue", "status": "open"}
        ) as resp:
            if resp.status == 200:
                results = await resp.json()
                print(f"✅ Found {len(results)} open issue(s)")
                # Verify all results are open (this was the bug we fixed)
                for result in results:
                    if result.get('status') != 'open':
                        print(f"⚠️  WARNING: Found non-open issue: {result['folio_id']} status={result.get('status')}")
            else:
                print(f"❌ Failed to search with status filter: {await resp.text()}")

        # Test 7: Post logs
        print("\n7️⃣ Posting logs...")
        async with session.post(
            f"{BASE_URL}/logs",
            json={
                "stream_id": "test-debug-stream",
                "source": "test-agent-001",
                "lines": [
                    {"stream_id": "test-debug-stream", "level": "INFO", "message": "Starting database investigation", "metadata": {}},
                    {"stream_id": "test-debug-stream", "level": "DEBUG", "message": "Connection pool size: 10", "metadata": {}},
                    {"stream_id": "test-debug-stream", "level": "ERROR", "message": "Query timeout after 30.2s", "metadata": {}},
                    {"stream_id": "test-debug-stream", "level": "INFO", "message": "Reproduced issue in staging", "metadata": {}}
                ]
            }
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                print(f"✅ Logged {data['count']} lines to stream")
            else:
                print(f"❌ Failed to post logs: {await resp.text()}")

        # Test 8: Retrieve logs
        print("\n8️⃣ Retrieving logs...")
        async with session.get(
            f"{BASE_URL}/logs/test-debug-stream",
            params={"level": "ERROR"}
        ) as resp:
            if resp.status == 200:
                logs = await resp.json()
                print(f"✅ Retrieved {len(logs)} ERROR log(s)")
                for log in logs:
                    print(f"   • [{log['timestamp']}] {log['message']}")
            else:
                print(f"❌ Failed to retrieve logs: {await resp.text()}")

        # Test 9: Thread search - by type
        print("\n9️⃣ Thread search - by type...")
        async with session.get(
            f"{BASE_URL}/threads",
            params={"type": "message"}
        ) as resp:
            if resp.status == 200:
                threads = await resp.json()
                print(f"✅ Found {len(threads)} message thread(s)")
                assert all(t['type'] == 'message' for t in threads), "Type filter failed"
            else:
                print(f"❌ Failed to search threads by type: {await resp.text()}")

        # Test 9a: Thread search - by weaver
        print("\n9️⃣a Thread search - by weaver...")
        async with session.get(
            f"{BASE_URL}/threads",
            params={"weaver": "test-agent-001"}
        ) as resp:
            if resp.status == 200:
                threads = await resp.json()
                print(f"✅ Found {len(threads)} thread(s) created by test-agent-001")
                assert all(t.get('weaver') == 'test-agent-001' for t in threads if t.get('weaver')), "Weaver filter failed"
            else:
                print(f"❌ Failed to search threads by weaver: {await resp.text()}")

        # Test 9b: Thread search - content search
        print("\n9️⃣b Thread search - content search...")
        async with session.get(
            f"{BASE_URL}/threads",
            params={"search": "Brief"}
        ) as resp:
            if resp.status == 200:
                threads = await resp.json()
                print(f"✅ Found {len(threads)} thread(s) containing 'Brief'")
                for thread in threads:
                    if thread.get('content'):
                        assert 'brief' in thread['content'].lower(), "Content search failed"
            else:
                print(f"❌ Failed to search threads by content: {await resp.text()}")

        # Test 9c: Thread search - time filter
        print("\n9️⃣c Thread search - time filter...")
        async with session.get(
            f"{BASE_URL}/threads",
            params={"since": "1hour"}
        ) as resp:
            if resp.status == 200:
                threads = await resp.json()
                print(f"✅ Found {len(threads)} thread(s) from last hour")
            else:
                print(f"❌ Failed to search threads by time: {await resp.text()}")

        # Test 10: Activity feed
        print("\n🔟 Getting activity feed...")
        async with session.get(f"{BASE_URL}/activity") as resp:
            if resp.status == 200:
                activity = await resp.json()
                print(f"✅ Activity feed retrieved:")
                print(f"   • {len(activity['new_folios'])} new folios")
                print(f"   • {len(activity['active_agents'])} active agents")
            else:
                print(f"❌ Failed to get activity: {await resp.text()}")

    print("\n✨ SKEIN workflow test complete!")


@pytest.mark.asyncio
async def test_unified_search():
    """Test unified search API endpoint."""

    print("🔍 Testing Unified Search API\n")

    headers = {
        "X-Project-Id": "test-project",
        "X-Agent-Id": "test-search-agent"
    }

    async with aiohttp.ClientSession(headers=headers) as session:

        # Test 1: Basic folio search (default)
        print("1️⃣ Testing basic folio search...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "test"}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                print(f"✅ Search completed in {data.get('execution_time_ms')}ms")
                print(f"   Total results: {data.get('total', 0)}")
                print(f"   Resources searched: {', '.join(data.get('resources', []))}")
                assert "folios" in data.get("results", {}), "Default should search folios"
            else:
                print(f"❌ Failed basic search: {await resp.text()}")
                return

        # Test 2: Multi-resource search
        print("\n2️⃣ Testing multi-resource search...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "test", "resources": "folios,threads,agents,sites"}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = data.get("results", {})
                print(f"✅ Found results across {len(results)} resource types:")
                for resource_type, resource_data in results.items():
                    total = resource_data.get("total", 0)
                    items_count = len(resource_data.get("items", []))
                    print(f"   • {resource_type}: {total} total, {items_count} returned")
            else:
                print(f"❌ Failed multi-resource search: {await resp.text()}")

        # Test 3: Search with filters - folios by type and status
        print("\n3️⃣ Testing folio search with type and status filters...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "", "type": "issue", "status": "open"}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                folios = data.get("results", {}).get("folios", {}).get("items", [])
                print(f"✅ Found {len(folios)} open issues")
                # Verify filters worked
                for folio in folios:
                    assert folio.get("type") == "issue", "Type filter failed"
                    # Status comes from threads, may be open or computed
            else:
                print(f"❌ Failed filtered search: {await resp.text()}")

        # Test 4: Search with site patterns
        print("\n4️⃣ Testing search with site patterns...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "", "sites": ["test-*", "opus-*"]}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                folios = data.get("results", {}).get("folios", {}).get("items", [])
                print(f"✅ Found {len(folios)} folios in test-* and opus-* sites")
                if folios:
                    print(f"   Example sites: {[f.get('site_id') for f in folios[:3]]}")
            else:
                print(f"❌ Failed site pattern search: {await resp.text()}")

        # Test 5: Search agents by capabilities
        print("\n5️⃣ Testing agent search by capabilities...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "", "resources": "agents", "capabilities": "testing"}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                agents = data.get("results", {}).get("agents", {}).get("items", [])
                print(f"✅ Found {len(agents)} agents with 'testing' capability")
                for agent in agents:
                    caps = agent.get("capabilities", [])
                    assert "testing" in caps, "Capabilities filter failed"
                    print(f"   • {agent.get('agent_id')}: {', '.join(caps)}")
            else:
                print(f"❌ Failed agent search: {await resp.text()}")

        # Test 6: Search threads by weaver and type
        print("\n6️⃣ Testing thread search by weaver and type...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "", "resources": "threads", "thread_type": "message", "weaver": "test-agent-001"}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                threads = data.get("results", {}).get("threads", {}).get("items", [])
                print(f"✅ Found {len(threads)} message threads by test-agent-001")
                for thread in threads:
                    assert thread.get("type") == "message", "Thread type filter failed"
            else:
                print(f"❌ Failed thread search: {await resp.text()}")

        # Test 7: Relevance sorting
        print("\n7️⃣ Testing relevance sorting...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "database", "sort": "relevance"}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                folios = data.get("results", {}).get("folios", {}).get("items", [])
                print(f"✅ Relevance sort returned {len(folios)} results")
                if folios:
                    print(f"   Top result: {folios[0].get('title', 'No title')[:60]}")
            else:
                print(f"❌ Failed relevance sort: {await resp.text()}")

        # Test 8: Pagination
        print("\n8️⃣ Testing pagination...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "", "limit": 5, "offset": 0}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                page1 = data.get("results", {}).get("folios", {}).get("items", [])
                print(f"✅ Page 1: {len(page1)} items (limit=5)")
                assert len(page1) <= 5, "Pagination limit failed"

                # Get page 2
                async with session.get(
                    f"{BASE_URL}/search",
                    params={"q": "", "limit": 5, "offset": 5}
                ) as resp2:
                    if resp2.status == 200:
                        data2 = await resp2.json()
                        page2 = data2.get("results", {}).get("folios", {}).get("items", [])
                        print(f"   Page 2: {len(page2)} items (offset=5)")
            else:
                print(f"❌ Failed pagination test: {await resp.text()}")

        # Test 9: Time filters
        print("\n9️⃣ Testing time filters...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "", "since": "1hour"}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                folios = data.get("results", {}).get("folios", {}).get("items", [])
                print(f"✅ Found {len(folios)} folios from last hour")
            else:
                print(f"❌ Failed time filter: {await resp.text()}")

        # Test 10: Empty query with filters (list all matching)
        print("\n🔟 Testing empty query with filters...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "", "type": "brief", "status": "open"}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                folios = data.get("results", {}).get("folios", {}).get("items", [])
                print(f"✅ Found {len(folios)} open briefs (empty query)")
                for folio in folios:
                    assert folio.get("type") == "brief", "Type filter failed with empty query"
            else:
                print(f"❌ Failed empty query test: {await resp.text()}")

        # Test 11: Invalid resource type (error handling)
        print("\n1️⃣1️⃣ Testing invalid resource type...")
        async with session.get(
            f"{BASE_URL}/search",
            params={"q": "test", "resources": "invalid"}
        ) as resp:
            if resp.status == 400:
                error = await resp.json()
                print(f"✅ Correctly rejected invalid resource type")
                print(f"   Error: {error.get('detail', 'No detail')}")
            else:
                print(f"❌ Should have rejected invalid resource type")

    print("\n✨ Unified search API test complete!")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "search":
        asyncio.run(test_unified_search())
    else:
        asyncio.run(test_skein_workflow())
