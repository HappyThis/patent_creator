import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import katex from 'katex';
import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const katexCss = await fs.readFile(path.join(frontendRoot, 'node_modules', 'katex', 'dist', 'katex.min.css'), 'utf8');
const mermaidPath = path.join(frontendRoot, 'node_modules', 'mermaid', 'dist', 'mermaid.min.js');

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

function normalizeMermaidSource(value) {
  return String(value || '').replace(/\\r\\n/g, '\n').replace(/\\n/g, '\n').replace(/\\t/g, '\t');
}

async function renderHtmlAsset(page, item, outputDir) {
  const displayMode = item.kind === 'block_formula';
  const katexResult = renderKatex(item.latex || '', displayMode);
  const fontSize = item.kind === 'inline_formula' ? '16px' : '17px';
  const padding = item.kind === 'inline_formula' ? '0 1px' : '8px 12px';
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

async function renderMermaidAsset(page, item, outputDir) {
  const source = normalizeMermaidSource(item.mermaid);
  const html = `<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
html, body {
  margin: 0;
  padding: 0;
  background: transparent;
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
#asset {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  width: 720px;
  height: 640px;
  padding: 18px;
  border: 1px solid rgba(92, 82, 68, 0.12);
  border-radius: 6px;
  background: rgba(255, 253, 248, 0.72);
  overflow: hidden;
}
#asset svg {
  display: block;
  max-width: 100%;
  max-height: 604px;
  height: auto;
  margin: 0 auto;
}
.missing {
  color: rgba(77, 72, 64, 0.54);
  font-size: 13px;
}
</style>
</head>
<body><div id="asset"><div class="missing">rendering</div></div></body>
</html>`;
  await page.setContent(html, { waitUntil: 'load' });
  try {
    await page.addScriptTag({ path: mermaidPath });
    await page.addScriptTag({ content: `
      window.__renderMermaid = async (source) => {
        const mermaid = window.mermaid;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: 'base',
          themeVariables: {
            background: '#fffdf8',
            primaryColor: '#fbf7ef',
            primaryBorderColor: '#c9b99d',
            primaryTextColor: '#1f2933',
            lineColor: '#6b7280',
            fontFamily: '-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif',
          },
          flowchart: {
            curve: 'basis',
            htmlLabels: false,
            nodeSpacing: 34,
            rankSpacing: 42,
          },
        });
        const result = await mermaid.render('docx_' + Math.random().toString(36).slice(2), source);
        return result.svg;
      };
    ` });
    await page.waitForFunction(() => typeof window.__renderMermaid === 'function', null, { timeout: 10000 });
    const svg = await page.evaluate(async (mermaidSource) => window.__renderMermaid(mermaidSource), source);
    await page.locator('#asset').evaluate((element, renderedSvg) => {
      element.innerHTML = renderedSvg;
    }, svg);
  } catch (error) {
    await page.locator('#asset').evaluate((element, message) => {
      element.replaceChildren();
      const missing = document.createElement('div');
      missing.className = 'missing';
      missing.textContent = message;
      element.appendChild(missing);
    }, error instanceof Error ? error.message : 'Mermaid render failed');
  }
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
      const result = item.kind === 'figure'
        ? await renderMermaidAsset(page, item, outputDir)
        : await renderHtmlAsset(page, item, outputDir);
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
