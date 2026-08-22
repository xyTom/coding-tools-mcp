import { mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = path.join(root, 'webui', 'src');
const outDir = path.join(root, 'coding_tools_mcp', 'webui_dist');
const adminScripts = [
  'settings-copy.js',
  'settings-model.js',
  'workspace-editor.js',
  'settings-page.js',
  'i18n.js',
  'admin.js',
];

async function buildPage(htmlName, styles, scripts) {
  let built = await readFile(path.join(srcDir, htmlName), 'utf8');
  for (const name of styles) {
    const source = await readFile(path.join(srcDir, name), 'utf8');
    built = built.replace(
      `<link rel="stylesheet" href="./${name}">`,
      `<style data-build-source="${name}">\n${source.trim()}\n</style>`,
    );
  }
  for (const name of scripts) {
    const source = await readFile(path.join(srcDir, name), 'utf8');
    built = built.replace(
      `<script type="module" src="./${name}"></script>`,
      `<script type="module" data-build-source="${name}">\n${source.trim()}\n</script>`,
    );
  }
  if (/<link\b[^>]*href=["'][^"']+\.css|<script\b[^>]*src=["'][^"']+\.js/i.test(built)) {
    throw new Error(`Build left an external WebUI asset reference in ${htmlName}.`);
  }
  return `${built.trim()}\n`;
}

const [adminBuilt, wikiBuilt] = await Promise.all([
  buildPage('admin.html', ['admin.css'], adminScripts),
  buildPage('wiki.html', ['wiki.css'], ['wiki.js']),
]);
await rm(outDir, { recursive: true, force: true });
await mkdir(outDir, { recursive: true });
await Promise.all([
  writeFile(path.join(outDir, 'admin.html'), adminBuilt, 'utf8'),
  writeFile(path.join(outDir, 'wiki.html'), wikiBuilt, 'utf8'),
]);
console.log('Built coding_tools_mcp/webui_dist/{admin,wiki}.html from webui/src/**');
