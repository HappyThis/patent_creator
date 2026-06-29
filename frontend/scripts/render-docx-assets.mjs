import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import katex from 'katex';
import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const katexCss = await fs.readFile(path.join(frontendRoot, 'node_modules', 'katex', 'dist', 'katex.min.css'), 'utf8');

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith('--')) {
      continue;
    }
    args[key.slice(2)] = argv[i + 1];
    i += 1;
  }
  return args;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderKatex(latex, displayMode) {
  try {
    return {
      ok: true,
      html: katex.renderToString(latex, {
        displayMode,
        throwOnError: true,
        strict: false,
      }),
    };
  } catch {
    return {
      ok: false,
      html: `<code>${escapeHtml(latex)}</code>`,
    };
  }
}

function slug(value) {
  return String(value).replace(/[^A-Za-z0-9_-]/g, '_');
}

async function renderHtmlAsset(page, item, outputDir) {
  const displayMode = item.kind === 'block_formula';
  const katexResult = renderKatex(item.latex || '', displayMode);
  const fontSize = item.kind === 'inline_formula' ? '16px' : '17px';
  const padding = item.kind === 'inline_formula' ? '1px 3px 2px 3px' : '10px 26px 12px 26px';
  const html = `<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
${katexCss}
html, body {
  margin: 0;
  padding: 0;
  background: transparent;
}
body {
  font-family: Calibri, "Segoe UI", Arial, sans-serif;
}
#asset {
  display: inline-block;
  box-sizing: border-box;
  padding: ${padding};
  color: #334155;
  font-size: ${fontSize};
  line-height: 1;
  background: transparent;
  vertical-align: baseline;
  overflow: visible;
}
#asset .katex-display {
  margin: 0;
}
#asset code {
  font-family: Consolas, monospace;
  font-size: 13px;
  color: #4a5763;
}
</style>
</head>
<body><div id="asset">${katexResult.html}</div></body>
</html>`;
  await page.setContent(html, { waitUntil: 'load' });
  const locator = page.locator('#asset');
  const assetPath = path.join(outputDir, `${slug(item.id)}.png`);
  await locator.screenshot({ path: assetPath, omitBackground: true });
  const box = await locator.boundingBox();
  return {
    id: item.id,
    kind: item.kind,
    path: assetPath,
    width_px: Math.ceil(box?.width ?? 1),
    height_px: Math.ceil(box?.height ?? 1),
    fallback: !katexResult.ok,
  };
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.input || !args.output || !args.manifest) {
    throw new Error('Usage: node render-docx-assets.mjs --input input.json --output output_dir --manifest manifest.json');
  }
  const inputPath = path.resolve(args.input);
  const outputDir = path.resolve(args.output);
  const manifestPath = path.resolve(args.manifest);
  await fs.mkdir(outputDir, { recursive: true });
  const payload = JSON.parse((await fs.readFile(inputPath, 'utf8')).replace(/^\uFEFF/, ''));
  const items = Array.isArray(payload.items) ? payload.items : [];
  const browser = await chromium.launch({ headless: true });
  const assets = {};
  try {
    const page = await browser.newPage({
      viewport: { width: 1200, height: 900 },
      deviceScaleFactor: 2,
    });
    for (const item of items) {
      if (item.kind !== 'inline_formula' && item.kind !== 'block_formula') {
        throw new Error(`Unsupported DOCX asset kind: ${item.kind}`);
      }
      const result = await renderHtmlAsset(page, item, outputDir);
      assets[item.id] = result;
    }
  } finally {
    await browser.close();
  }
  await fs.writeFile(manifestPath, JSON.stringify({ assets }, null, 2), 'utf8');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
