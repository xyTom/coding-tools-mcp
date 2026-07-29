import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = path.join(root, 'webui', 'src');
const outDir = path.join(root, 'coding_tools_mcp', 'webui_dist');
const outFile = path.join(outDir, 'admin.html');
const outCss = path.join(outDir, 'admin.css');
const jsFiles = ['admin.js', 'settings-model.js', 'settings-copy.js', 'workspace-editor.js', 'settings-page.js'];

const [html, css, modules] = await Promise.all([
  readFile(path.join(srcDir, 'admin.html'), 'utf8'),
  readFile(path.join(srcDir, 'admin.css'), 'utf8'),
  Promise.all(jsFiles.map(async (file) => [file, await readFile(path.join(srcDir, file), 'utf8')])),
]);

const built = html
  .replace('<link rel="stylesheet" href="./admin.css">', '<link rel="stylesheet" href="/admin/assets/admin.css">')
  .replace('<script type="module" src="./admin.js"></script>', '<script type="module" src="/admin/assets/admin.js"></script>');

await mkdir(outDir, { recursive: true });
await Promise.all([
  writeFile(outFile, built, 'utf8'),
  writeFile(outCss, css.trim() + '\n', 'utf8'),
  ...modules.map(([file, source]) => writeFile(path.join(outDir, file), source.trim() + '\n', 'utf8')),
]);

console.log(`Built ${path.relative(root, outFile)}`);
