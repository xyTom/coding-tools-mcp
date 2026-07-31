import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { containsCredentialControl, sanitizeAdminValue } from '../src/admin.js';

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
});
