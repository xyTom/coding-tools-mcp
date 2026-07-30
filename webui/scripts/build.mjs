import { mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = path.join(root, 'webui', 'src');
const outDir = path.join(root, 'coding_tools_mcp', 'webui_dist');
const scripts = [
  'settings-copy.js',
  'settings-model.js',
  'workspace-editor.js',
  'settings-page.js',
  'admin.js',
];

const [html, css, modules] = await Promise.all([
  readFile(path.join(srcDir, 'admin.html'), 'utf8'),
  readFile(path.join(srcDir, 'admin.css'), 'utf8'),
  Promise.all(scripts.map(async (name) => [name, await readFile(path.join(srcDir, name), 'utf8')])),
]);

let built = html.replace(
  '<link rel="stylesheet" href="./admin.css">',
  `<style data-build-source="admin.css">\n${css.trim()}\n</style>`,
);
for (const [name, source] of modules) {
  built = built.replace(
    `<script type="module" src="./${name}"></script>`,
    `<script type="module" data-build-source="${name}">\n${source.trim()}\n</script>`,
  );
}
if (/<link\b[^>]*href=["'][^"']+\.css|<script\b[^>]*src=["'][^"']+\.js/i.test(built)) {
  throw new Error('Build left an external WebUI asset reference in admin.html.');
}
await rm(outDir, { recursive: true, force: true });
await mkdir(outDir, { recursive: true });
await writeFile(path.join(outDir, 'admin.html'), `${built.trim()}\n`, 'utf8');
console.log('Built coding_tools_mcp/webui_dist/admin.html from webui/src/**');
