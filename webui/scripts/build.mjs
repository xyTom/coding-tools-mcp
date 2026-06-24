import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = path.join(root, 'webui', 'src');
const outDir = path.join(root, 'coding_tools_mcp', 'webui_dist');
const outFile = path.join(outDir, 'admin.html');

const [html, css, js] = await Promise.all([
  readFile(path.join(srcDir, 'admin.html'), 'utf8'),
  readFile(path.join(srcDir, 'admin.css'), 'utf8'),
  readFile(path.join(srcDir, 'admin.js'), 'utf8'),
]);

const built = html
  .replace('<link rel="stylesheet" href="./admin.css">', `<style>\n${css.trim()}\n</style>`)
  .replace('<script type="module" src="./admin.js"></script>', `<script>\n${js.trim()}\n</script>`);

await mkdir(outDir, { recursive: true });
await writeFile(outFile, built, 'utf8');

console.log(`Built ${path.relative(root, outFile)}`);
