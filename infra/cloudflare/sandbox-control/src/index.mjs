const JSON_CONTENT_TYPE = "application/json; charset=utf-8";
const DEFAULT_WORKFLOW_ID = "start-sandbox.yml";
const DEFAULT_IMAGE = "ghcr.io/xytom/coding-tools-mcp-sandbox:latest";
const DEFAULT_PORT = "8765";
const DEFAULT_DURATION_MINUTES = "120";
const DEFAULT_PERMISSION_MODE = "trusted";
const DEFAULT_TUNNEL_TYPE = "named";

const PERMISSION_MODES = new Set(["safe", "trusted", "dangerous"]);
const TUNNEL_TYPES = new Set(["quick", "named"]);

const START_TOOL = {
  name: "start_coding_tools_sandbox",
  description: "Trigger the GitHub Actions workflow that starts a coding-tools-mcp Docker sandbox and exposes it through Cloudflare Tunnel.",
  inputSchema: {
    type: "object",
    properties: {
      ref: { type: "string", description: "Git ref to run the workflow on. Defaults to GITHUB_REF." },
      duration_minutes: { type: "string", description: "Sandbox lifetime, 5-330 minutes." },
      permission_mode: { type: "string", enum: [...PERMISSION_MODES] },
      tunnel_type: { type: "string", enum: [...TUNNEL_TYPES] },
      tunnel_hostname: { type: "string", description: "Stable hostname for tunnel_type=named, for example mcp.example.com." },
      checkout_repository: { type: "boolean" },
    },
    additionalProperties: false,
  },
};

const STATUS_TOOL = {
  name: "get_coding_tools_sandbox_status",
  description: "Query the GitHub Actions workflow run status for a coding-tools-mcp sandbox and optionally probe the fixed MCP tunnel endpoint.",
  inputSchema: {
    type: "object",
    properties: {
      run_id: { type: "string", description: "Workflow run ID returned by start_coding_tools_sandbox. If omitted, the latest workflow_dispatch run is returned." },
      ref: { type: "string", description: "Branch/ref to search when run_id is omitted. Defaults to GITHUB_REF." },
      workflow_id: { type: "string", description: "Workflow YAML filename. Defaults to WORKFLOW_ID." },
      per_page: { type: "integer", minimum: 1, maximum: 20, description: "Number of recent runs to return when run_id is omitted. Defaults to 5." },
      check_endpoint: { type: "boolean", description: "Probe https://<tunnel_hostname>/mcp to see whether the tunnel endpoint is responding." },
      tunnel_hostname: { type: "string", description: "Hostname to probe when check_endpoint=true. Defaults to TUNNEL_HOSTNAME." },
    },
    additionalProperties: false,
  },
};

const CONTROL_TOOLS = [START_TOOL, STATUS_TOOL];

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);

      if (request.method === "GET" && url.pathname === "/") {
        return jsonResponse({ ok: true, service: "coding-tools-sandbox-control" });
      }

      const auth = await requireBearerAuth(request, env.CONTROL_TOKEN);
      if (auth) return auth;

      if (url.pathname === "/start") {
        if (request.method !== "POST") return methodNotAllowed("POST");
        const body = await readJsonBody(request);
        const result = await startSandbox(body ?? {}, env);
        return jsonResponse(result, { status: 202 });
      }

      if (url.pathname === "/mcp") {
        if (request.method !== "POST") return methodNotAllowed("POST");
        const body = await readJsonBody(request);
        return handleMcpRequest(body, env);
      }

      return jsonResponse({ error: "not_found" }, { status: 404 });
    } catch (error) {
      const status = error instanceof HttpError ? error.status : 500;
      const code = error instanceof HttpError ? error.code : "internal_error";
      return jsonResponse({ error: code, message: error.message }, { status });
    }
  },
};

async function handleMcpRequest(body, env) {
  if (!body || typeof body !== "object") {
    throw new HttpError(400, "invalid_json_rpc", "Expected a JSON-RPC request body.");
  }

  const id = body.id ?? null;
  if (body.jsonrpc !== "2.0" || typeof body.method !== "string") {
    return jsonRpcError(id, -32600, "Invalid Request");
  }

  if (id === null && body.method.startsWith("notifications/")) {
    return new Response(null, { status: 202 });
  }

  if (body.method === "initialize") {
    return jsonRpcResult(id, {
      protocolVersion: "2025-11-25",
      capabilities: { tools: {} },
      serverInfo: { name: "coding-tools-sandbox-control", version: "0.1.0" },
    });
  }

  if (body.method === "ping") {
    return jsonRpcResult(id, {});
  }

  if (body.method === "resources/list") {
    return jsonRpcResult(id, { resources: [] });
  }

  if (body.method === "prompts/list") {
    return jsonRpcResult(id, { prompts: [] });
  }

  if (body.method === "tools/list") {
    return jsonRpcResult(id, { tools: CONTROL_TOOLS });
  }

  if (body.method === "tools/call") {
    const name = body.params?.name;

    try {
      const args = body.params?.arguments ?? {};
      let result;
      if (name === START_TOOL.name) {
        result = await startSandbox(args, env);
      } else if (name === STATUS_TOOL.name) {
        result = await getSandboxStatus(args, env);
      } else {
        return jsonRpcError(id, -32602, `Unknown tool: ${String(name)}`);
      }

      return jsonRpcResult(id, {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
        structuredContent: result,
      });
    } catch (error) {
      return jsonRpcResult(id, {
        isError: true,
        content: [{ type: "text", text: error.message }],
      });
    }
  }

  return jsonRpcError(id, -32601, "Method not found");
}

async function startSandbox(input, env) {
  const owner = requiredEnv(env, "GITHUB_OWNER");
  const repo = requiredEnv(env, "GITHUB_REPO");
  const githubToken = requiredEnv(env, "GITHUB_TOKEN");
  const ref = cleanRef(input.ref ?? env.GITHUB_REF ?? "main");
  const workflowId = cleanWorkflowId(input.workflow_id ?? env.WORKFLOW_ID ?? DEFAULT_WORKFLOW_ID);
  const inputs = buildWorkflowInputs(input, env);
  const mcpUrl = inputs.tunnel_type === "named" ? `https://${inputs.tunnel_hostname}/mcp` : null;

  const endpoint = `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/actions/workflows/${encodeURIComponent(workflowId)}/dispatches`;
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { ...githubHeaders(githubToken), "Content-Type": "application/json" },
    body: JSON.stringify({ ref, inputs }),
  });

  let workflowRun = null;
  if (response.status === 200) {
    workflowRun = await response.json().catch(() => null);
  } else if (response.status !== 204) {
    const text = await response.text();
    throw new HttpError(response.status, "github_dispatch_failed", formatGitHubDispatchError(text, ref, workflowId, response.status));
  }

  return {
    ok: true,
    github_status: response.status,
    owner,
    repo,
    workflow_id: workflowId,
    ref,
    inputs,
    mcp_url: mcpUrl,
    mcp_authorization: mcpUrl ? "Bearer <CODING_TOOLS_MCP_AUTH_TOKEN>" : null,
    workflow_run: workflowRun,
    message: "Sandbox workflow dispatch accepted by GitHub.",
  };
}

async function getSandboxStatus(input, env) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new HttpError(400, "invalid_input", "Tool arguments must be a JSON object.");
  }

  const owner = requiredEnv(env, "GITHUB_OWNER");
  const repo = requiredEnv(env, "GITHUB_REPO");
  const githubToken = requiredEnv(env, "GITHUB_TOKEN");
  const workflowId = cleanWorkflowId(input.workflow_id ?? env.WORKFLOW_ID ?? DEFAULT_WORKFLOW_ID);
  const ref = input.ref == null || input.ref === "" ? cleanRef(env.GITHUB_REF ?? "main") : cleanRef(input.ref);
  const runId = cleanRunId(input.run_id ?? "");
  const perPage = runId ? 1 : cleanPerPage(input.per_page ?? 5);
  const checkEndpoint = cleanOptionalBoolean(input.check_endpoint ?? false, "check_endpoint");
  const tunnelHostname = cleanOptionalHostname(input.tunnel_hostname ?? env.TUNNEL_HOSTNAME ?? "");
  // Start the tunnel probe now so it runs concurrently with the GitHub API
  // calls below; probeMcpEndpoint never rejects.
  const endpointProbePromise = checkEndpoint ? probeMcpEndpoint(tunnelHostname, env) : null;

  let latestRun = null;
  let recentRuns = [];
  if (runId) {
    const endpoint = `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/actions/runs/${encodeURIComponent(runId)}`;
    latestRun = await fetchGitHubJson(endpoint, githubToken, "github_run_status_failed");
  } else {
    const endpoint = new URL(`https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/actions/workflows/${encodeURIComponent(workflowId)}/runs`);
    endpoint.searchParams.set("branch", ref);
    endpoint.searchParams.set("event", "workflow_dispatch");
    endpoint.searchParams.set("per_page", String(perPage));
    const data = await fetchGitHubJson(endpoint.toString(), githubToken, "github_run_status_failed");
    recentRuns = Array.isArray(data?.workflow_runs) ? data.workflow_runs : [];
    latestRun = recentRuns[0] ?? null;
  }

  const run = summarizeWorkflowRun(latestRun);
  const endpointProbe = endpointProbePromise ? await endpointProbePromise : null;
  const mcpUrl = tunnelHostname ? `https://${tunnelHostname}/mcp` : null;

  return {
    ok: true,
    owner,
    repo,
    workflow_id: workflowId,
    ref,
    run_id: run?.id ?? (runId || null),
    action_state: deriveActionState(run, endpointProbe),
    run,
    recent_runs: runId ? undefined : recentRuns.map(summarizeWorkflowRun),
    mcp_url: mcpUrl,
    endpoint_probe: endpointProbe,
    message: run ? "Sandbox workflow run status fetched from GitHub." : "No matching sandbox workflow runs found.",
  };
}

function githubHeaders(githubToken) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${githubToken}`,
    "User-Agent": "coding-tools-sandbox-control-worker",
    "X-GitHub-Api-Version": "2026-03-10",
  };
}

async function fetchGitHubJson(endpoint, githubToken, errorCode) {
  const response = await fetch(endpoint, {
    method: "GET",
    headers: githubHeaders(githubToken),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new HttpError(response.status, errorCode, formatGitHubApiError(text, response.status));
  }
  return text ? JSON.parse(text) : null;
}

function formatGitHubApiError(text, status) {
  if (!text) return `GitHub returned HTTP ${status}`;
  try {
    const parsed = JSON.parse(text);
    return parsed?.message ? String(parsed.message) : text;
  } catch {
    return text;
  }
}

function summarizeWorkflowRun(run) {
  if (!run || typeof run !== "object") return null;
  return {
    id: run.id ?? null,
    name: run.name ?? null,
    display_title: run.display_title ?? null,
    status: run.status ?? null,
    conclusion: run.conclusion ?? null,
    event: run.event ?? null,
    head_branch: run.head_branch ?? null,
    head_sha: run.head_sha ?? null,
    run_number: run.run_number ?? null,
    run_attempt: run.run_attempt ?? null,
    html_url: run.html_url ?? null,
    created_at: run.created_at ?? null,
    updated_at: run.updated_at ?? null,
    run_started_at: run.run_started_at ?? null,
    jobs_url: run.jobs_url ?? null,
    logs_url: run.logs_url ?? null,
  };
}

function deriveActionState(run, endpointProbe) {
  if (!run) return "not_found";
  if (["queued", "requested", "waiting", "pending"].includes(run.status)) return "queued";
  if (run.status === "in_progress") {
    if (endpointProbe?.mcp_ready) return "mcp_ready";
    if (endpointProbe) return "action_running_mcp_not_ready";
    return "action_running";
  }
  if (run.status === "completed") {
    return run.conclusion ? `completed_${run.conclusion}` : "completed";
  }
  return run.status ?? "unknown";
}

async function probeMcpEndpoint(tunnelHostname, env) {
  if (!tunnelHostname) {
    return { checked: false, mcp_ready: false, reachable: false, error: "tunnel_hostname is not configured." };
  }

  const mcpUrl = `https://${tunnelHostname}/mcp`;
  const headers = { "Content-Type": "application/json" };
  if (env.CODING_TOOLS_MCP_AUTH_TOKEN) {
    headers.Authorization = `Bearer ${env.CODING_TOOLS_MCP_AUTH_TOKEN}`;
  }

  try {
    const startedAt = Date.now();
    const response = await fetch(mcpUrl, {
      method: "POST",
      headers,
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping", params: {} }),
    });
    const elapsed_ms = Date.now() - startedAt;
    const mcpReady = response.status === 200 || response.status === 401;
    return {
      checked: true,
      url: mcpUrl,
      status: response.status,
      reachable: response.status < 500,
      mcp_ready: mcpReady,
      authenticated: response.status === 200 && Boolean(env.CODING_TOOLS_MCP_AUTH_TOKEN),
      elapsed_ms,
    };
  } catch (error) {
    return {
      checked: true,
      url: mcpUrl,
      reachable: false,
      mcp_ready: false,
      error: error.message,
    };
  }
}

function formatGitHubDispatchError(text, ref, workflowId, status) {
  if (!text) return `GitHub returned HTTP ${status}`;

  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    return text;
  }

  const message = parsed?.message ? String(parsed.message) : text;
  if (status === 422 && message.includes("Unexpected inputs")) {
    return `${message}. The workflow '${workflowId}' at ref '${ref}' does not declare one or more inputs sent by this Worker. Merge the updated workflow into that ref, set GITHUB_REF to a branch that has it, or pass a matching 'ref' in the request.`;
  }

  return text;
}

function buildWorkflowInputs(input, env) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new HttpError(400, "invalid_input", "Request body must be a JSON object.");
  }

  const permissionMode = cleanEnum(input.permission_mode ?? env.DEFAULT_PERMISSION_MODE ?? DEFAULT_PERMISSION_MODE, PERMISSION_MODES, "permission_mode");
  if (permissionMode === "dangerous" && env.ALLOW_DANGEROUS !== "true") {
    throw new HttpError(400, "dangerous_mode_disabled", "Set ALLOW_DANGEROUS=true to allow permission_mode=dangerous.");
  }

  const tunnelType = cleanEnum(input.tunnel_type ?? env.DEFAULT_TUNNEL_TYPE ?? DEFAULT_TUNNEL_TYPE, TUNNEL_TYPES, "tunnel_type");
  const tunnelHostname = cleanHostname(input.tunnel_hostname ?? env.TUNNEL_HOSTNAME ?? "", tunnelType);
  const defaultImage = env.DEFAULT_IMAGE ?? DEFAULT_IMAGE;
  const image = cleanImage(input.image ?? defaultImage, defaultImage, env.ALLOW_IMAGE_OVERRIDE === "true");

  return {
    image,
    port: cleanPort(input.port ?? env.DEFAULT_PORT ?? DEFAULT_PORT),
    permission_mode: permissionMode,
    checkout_repository: cleanBoolean(input.checkout_repository ?? true, "checkout_repository"),
    duration_minutes: cleanDuration(input.duration_minutes ?? env.DEFAULT_DURATION_MINUTES ?? DEFAULT_DURATION_MINUTES),
    auth_token: "",
    hide_auth_token: true,
    tunnel_type: tunnelType,
    tunnel_hostname: tunnelHostname,
  };
}

async function requireBearerAuth(request, expectedToken) {
  if (!expectedToken) {
    return jsonResponse({ error: "server_misconfigured", message: "CONTROL_TOKEN secret is not configured." }, { status: 500 });
  }

  const header = request.headers.get("Authorization") ?? "";
  const match = header.match(/^Bearer\s+(.+)$/i);
  if (!match || !(await secureEqual(match[1], expectedToken))) {
    return jsonResponse({ error: "unauthorized" }, { status: 401, headers: { "WWW-Authenticate": "Bearer" } });
  }
  return null;
}

async function secureEqual(a, b) {
  const encoder = new TextEncoder();
  const [left, right] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(String(a))),
    crypto.subtle.digest("SHA-256", encoder.encode(String(b))),
  ]);
  const leftBytes = new Uint8Array(left);
  const rightBytes = new Uint8Array(right);
  let diff = leftBytes.length ^ rightBytes.length;
  for (let i = 0; i < Math.max(leftBytes.length, rightBytes.length); i += 1) {
    diff |= (leftBytes[i] ?? 0) ^ (rightBytes[i] ?? 0);
  }
  return diff === 0;
}

async function readJsonBody(request) {
  const text = await request.text();
  if (!text.trim()) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, "invalid_json", "Request body must be valid JSON.");
  }
}

function cleanDuration(value) {
  const duration = cleanString(value, "duration_minutes");
  if (!/^\d+$/.test(duration)) throw new HttpError(400, "invalid_duration", "duration_minutes must be an integer string.");
  const numeric = Number(duration);
  if (numeric < 5 || numeric > 330) throw new HttpError(400, "invalid_duration", "duration_minutes must be between 5 and 330.");
  return duration;
}

function cleanPort(value) {
  const port = cleanString(value, "port");
  if (!/^\d+$/.test(port)) throw new HttpError(400, "invalid_port", "port must be an integer string.");
  const numeric = Number(port);
  if (numeric < 1 || numeric > 65535) throw new HttpError(400, "invalid_port", "port must be between 1 and 65535.");
  return port;
}

function cleanEnum(value, allowed, name) {
  const text = cleanString(value, name);
  if (!allowed.has(text)) throw new HttpError(400, `invalid_${name}`, `${name} must be one of: ${[...allowed].join(", ")}.`);
  return text;
}

function cleanHostname(value, tunnelType) {
  if (tunnelType !== "named") return "";
  const hostname = cleanOptionalHostname(value);
  if (!hostname) throw new HttpError(400, "missing_tunnel_hostname", "tunnel_hostname is required when tunnel_type=named.");
  return hostname;
}

function cleanImage(value, defaultImage, allowOverride) {
  const image = cleanString(value, "image");
  if (!allowOverride && image !== defaultImage) {
    throw new HttpError(400, "image_override_disabled", "Set ALLOW_IMAGE_OVERRIDE=true to dispatch a non-default image.");
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,255}$/.test(image)) {
    throw new HttpError(400, "invalid_image", "image contains unsupported characters.");
  }
  return image;
}

function cleanRef(value) {
  const ref = cleanString(value, "ref");
  if (!/^[A-Za-z0-9._/@+-]{1,255}$/.test(ref)) throw new HttpError(400, "invalid_ref", "ref contains unsupported characters.");
  return ref;
}

function cleanWorkflowId(value) {
  const workflowId = cleanString(value, "workflow_id");
  if (!/^[A-Za-z0-9._-]+\.(ya?ml)$/.test(workflowId)) {
    throw new HttpError(400, "invalid_workflow_id", "workflow_id must be a workflow YAML filename, for example start-sandbox.yml.");
  }
  return workflowId;
}

function cleanRunId(value) {
  if (value == null || value === "") return "";
  const text = String(value).trim();
  if (!/^\d+$/.test(text)) throw new HttpError(400, "invalid_run_id", "run_id must be an integer string.");
  return text;
}

function cleanPerPage(value) {
  const number = typeof value === "number" ? value : Number(String(value).trim());
  if (!Number.isInteger(number) || number < 1 || number > 20) {
    throw new HttpError(400, "invalid_per_page", "per_page must be an integer between 1 and 20.");
  }
  return number;
}

function cleanOptionalHostname(value) {
  const hostname = String(value ?? "").replace(/^https?:\/\//, "").split("/")[0].trim();
  if (!hostname) return "";
  if (!/^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/.test(hostname)) {
    throw new HttpError(400, "invalid_tunnel_hostname", "tunnel_hostname must be a valid hostname, for example mcp.example.com.");
  }
  return hostname;
}

function cleanOptionalBoolean(value, name) {
  if (value == null || value === "") return false;
  return cleanBoolean(value, name);
}

function cleanBoolean(value, name) {
  if (typeof value === "boolean") return value;
  if (value === "true") return true;
  if (value === "false") return false;
  throw new HttpError(400, `invalid_${name}`, `${name} must be a boolean.`);
}

function cleanString(value, name) {
  if (typeof value !== "string") throw new HttpError(400, `invalid_${name}`, `${name} must be a string.`);
  const text = value.trim();
  if (!text) throw new HttpError(400, `missing_${name}`, `${name} is required.`);
  return text;
}

function requiredEnv(env, name) {
  const value = env[name];
  if (!value) throw new HttpError(500, "server_misconfigured", `${name} is not configured.`);
  return value;
}

function methodNotAllowed(method) {
  return jsonResponse({ error: "method_not_allowed" }, { status: 405, headers: { Allow: method } });
}

function jsonRpcResult(id, result) {
  return jsonResponse({ jsonrpc: "2.0", id, result });
}

function jsonRpcError(id, code, message) {
  return jsonResponse({ jsonrpc: "2.0", id, error: { code, message } });
}

function jsonResponse(body, init = {}) {
  const headers = new Headers(init.headers ?? {});
  headers.set("Content-Type", JSON_CONTENT_TYPE);
  headers.set("Cache-Control", "no-store");
  return new Response(JSON.stringify(body), { ...init, headers });
}

class HttpError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.code = code;
  }
}
