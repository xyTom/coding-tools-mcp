import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  containsCredentialControl,
  gatewayExposurePreview,
  gatewayServerFromForm,
  gatewayServerTemplate,
  restartImpactCount,
  sanitizeAdminValue,
} from '../src/admin.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('admin response sanitizer removes secret material recursively', () => {
  const raw = {
    client_id: 'agent',
    client_secret_digest: 'digest-canary',
    nested: {
      token_hash: 'hash-canary',
      secret_ref: 'vault/reference-canary',
      access_token: 'access-canary',
      token_endpoint_auth_method: 'client_secret_post',
    },
  };
  const safe = sanitizeAdminValue(raw);
  const encoded = JSON.stringify(safe);
  assert.equal(safe.client_id, 'agent');
  assert.equal(safe.nested.token_endpoint_auth_method, 'client_secret_post');
  for (const canary of ['digest-canary', 'hash-canary', 'reference-canary', 'access-canary']) {
    assert.equal(encoded.includes(canary), false);
  }
});

test('gateway WebUI rejects credential and reference control fields', () => {
  assert.equal(containsCredentialControl({ servers: { x: { command: 'node' } } }), false);
  assert.equal(containsCredentialControl({ servers: { x: { env: { TOKEN: { secret_ref: 'name' } } } } }), true);
  assert.equal(containsCredentialControl({ servers: { x: { headers: { Authorization: 'Bearer canary' } } } }), true);
});

test('new Gateway server template defaults to restart-only broker exposure', () => {
  const template = gatewayServerTemplate('chemistry');
  assert.equal(template.servers.chemistry.expose_mode, 'broker');
  assert.deepEqual(template.servers.chemistry.pinned_tools, []);
  assert.deepEqual(template.tool_search.custom_synonyms, {});
});

test('restart impact count never double counts settings restart_required', () => {
  assert.equal(
    restartImpactCount(
      { pendingRestart: ['host', 'permission_mode'], restartRequired: true },
      { restart_required: false },
    ),
    2,
  );
  assert.equal(
    restartImpactCount(
      { pendingRestart: [], restartRequired: false },
      { restart_required: true },
    ),
    1,
  );
  assert.equal(
    restartImpactCount(
      { pendingRestart: ['host', 'permission_mode'], restartRequired: true },
      { restart_required: true },
    ),
    3,
  );
});

test('Gateway form builds broker and startup-enable configuration without requiring JSON', () => {
  const result = gatewayServerFromForm({
    alias: 'chemistry',
    transport: 'stdio',
    command: 'npx',
    args: '-y\n@scope/chemistry-mcp',
    environment: 'CACHE_DIR=C:\\data\nAPI_TOKEN=secret:chemistry/token\nPROFILE=env:USERPROFILE',
    enabled: true,
    exposeMode: 'broker',
    pinnedTools: 'search_sds\nsearch_literature',
    includeTools: '',
    excludeTools: 'delete_record',
    tags: 'chemistry\nliterature',
    timeoutMs: '45000',
  });
  assert.equal(result.alias, 'chemistry');
  assert.deepEqual(result.config.args, ['-y', '@scope/chemistry-mcp']);
  assert.deepEqual(result.config.env, {
    CACHE_DIR: 'C:\\data',
    API_TOKEN: { secret_ref: 'chemistry/token' },
    PROFILE: { env_ref: 'USERPROFILE' },
  });
  assert.equal(result.config.enabled, true);
  assert.equal(result.config.expose_mode, 'broker');
  assert.deepEqual(result.config.pinned_tools, ['search_sds', 'search_literature']);
  assert.equal(result.config.timeout_ms, 45000);
});

test('Gateway form rejects invalid aliases and environment rows early', () => {
  assert.throws(() => gatewayServerFromForm({ alias: 'bad alias', transport: 'streamable_http', url: 'https://example.test/mcp' }), /alias/i);
  assert.throws(() => gatewayServerFromForm({ alias: 'valid', transport: 'stdio', command: 'uvx', environment: 'MISSING_SEPARATOR' }), /KEY=value/);
});

test('Gateway exposure preview consumes the real aggregate backend contract', () => {
  const activeStatus = {
    exposure_report: {
      servers: [{
        alias: 'remote',
        catalog_count: 3,
        direct_count: 1,
        broker_only_count: 2,
        definition_bytes: 812,
      }],
    },
  };
  const direct = gatewayExposurePreview({ servers: { remote: {} } }, activeStatus)[0];
  assert.equal(direct.catalog_count, 3);
  assert.equal(direct.direct_count, 1);
  assert.equal(direct.broker_only_count, 2);
  assert.equal(direct.definition_bytes, 812);

  const broker = gatewayExposurePreview({
    servers: {
      remote: {
        expose_mode: 'broker',
        include_tools: ['search', 'create_issue'],
        pinned_tools: ['search'],
      },
    },
  }, activeStatus)[0];
  assert.deepEqual(broker.configured_pins, ['search']);
  assert.deepEqual(broker.configured_include, ['create_issue', 'search']);
  assert.deepEqual(broker.configured_exclude, []);
  assert.equal('tools' in broker, false);
});

test('WebUI source has no obsolete catalog controls or unsafe rendering/storage paths', async () => {
  const srcDir = path.join(root, 'src');
  const files = (await readdir(srcDir)).filter((name) => name.endsWith('.js') || name.endsWith('.html'));
  const source = (await Promise.all(files.map((name) => readFile(path.join(srcDir, name), 'utf8')))).join('\n');
  assert.doesNotMatch(source, /tool_profile/i);
  assert.doesNotMatch(source, /innerHTML/);
  assert.doesNotMatch(source, /localStorage|sessionStorage/);
  assert.doesNotMatch(source, /reload_upstream|start_server|stop_server/);
  assert.doesNotMatch(source, /mcpAdminToken/);
});

test('package uses no latest dependencies and build is source-only', async () => {
  const packageJson = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));
  assert.equal(packageJson.devDependencies, undefined);
  const build = await readFile(path.join(root, 'scripts', 'build.mjs'), 'utf8');
  assert.match(build, /webui.*src/);
  assert.match(build, /webui_dist/);
  assert.doesNotMatch(build, /coding_tools_mcp.*webui_dist.*readFile/);
});


test('HTML labels controls, links errors, and includes narrow-screen layout', async () => {
  const html = await readFile(path.join(root, 'src', 'admin.html'), 'utf8');
  const css = await readFile(path.join(root, 'src', 'admin.css'), 'utf8');
  const controls = [...html.matchAll(/<(?:input|select|textarea)\b[^>]*\bid="([^"]+)"[^>]*>/g)].map((match) => match[1]);
  for (const id of controls) {
    assert.match(html, new RegExp(`<label[^>]*for="${id}"|<label[^>]*>[\\s\\S]*?id="${id}"`), `missing label for ${id}`);
  }
  assert.match(html, /id="settingsError"[^>]*role="alert"/);
  assert.match(html, /id="settingsConflict"[^>]*role="alert"/);
  assert.match(css, /@media\s*\(max-width:\s*560px\)/);
  assert.match(css, /min-height:\s*44px/);
  assert.match(html, /id="gatewayServerForm"/);
  assert.match(html, /id="gatewayExposeMode"/);
  assert.match(html, /id="gatewayEnabled"/);
  assert.match(html, /id="gatewayAdvanced"/);
  assert.match(html, /oauth\/authorization-password/);
  assert.match(html, /id="clientPasswordDialog"/);
  assert.match(html, /id="clientPasswordValue"[^>]*type="password"/);
  assert.match(html, /data-password-toggle/);
  assert.doesNotMatch(html, /<h3>Restart-only JSON<\/h3>/);
});
