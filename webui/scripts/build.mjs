import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = path.join(root, 'webui', 'src');
const outDir = path.join(root, 'coding_tools_mcp', 'webui_dist');
const outFile = path.join(outDir, 'admin.html');
const outCss = path.join(outDir, 'admin.css');
const outJs = path.join(outDir, 'admin.js');

const [html, css, js] = await Promise.all([
  readFile(path.join(srcDir, 'admin.html'), 'utf8'),
  readFile(path.join(srcDir, 'admin.css'), 'utf8'),
  readFile(path.join(srcDir, 'admin.js'), 'utf8'),
]);

const built = html
  .replace('<link rel="stylesheet" href="./admin.css">', '<link rel="stylesheet" href="/admin/assets/admin.css">')
  .replace('<script type="module" src="./admin.js"></script>', '<script defer src="/admin/assets/admin.js"></script>');

await mkdir(outDir, { recursive: true });
await Promise.all([
  writeFile(outFile, built, 'utf8'),
  writeFile(outCss, css.trim() + '\n', 'utf8'),
  writeFile(outJs, js.trim() + '\n', 'utf8'),
]);

console.log(`Built ${path.relative(root, outFile)}`);
