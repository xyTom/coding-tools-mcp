import os

# Tests exercise real MCP handshakes; a test run must never send product
# telemetry, regardless of how the suite is invoked (make, IDE, unittest).
os.environ.setdefault("CODING_TOOLS_MCP_TELEMETRY", "off")
