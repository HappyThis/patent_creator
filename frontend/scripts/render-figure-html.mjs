import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright';

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

async function main() {
  const args = parseArgs(process.argv);
  if (!args.input || !args.output) {
    throw new Error('Usage: node render-figure-html.mjs --input diagram.html --output render.png --width 1500 --height 900');
  }

  const inputPath = path.resolve(args.input);
  const outputPath = path.resolve(args.output);
  const width = Number.parseInt(args.width || '1500', 10);
  const height = Number.parseInt(args.height || '900', 10);
  if (!Number.isFinite(width) || width <= 0 || !Number.isFinite(height) || height <= 0) {
    throw new Error('Invalid figure viewport size.');
  }

  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  const browser = await chromium.launch({ headless: true });
  try {
    const inputUrl = pathToFileURL(inputPath).href;
    const page = await browser.newPage({
      viewport: { width, height },
      deviceScaleFactor: 1,
    });
    await page.route('**/*', (route) => {
      const requestUrl = route.request().url();
      if (requestUrl === inputUrl || requestUrl.startsWith('data:') || requestUrl === 'about:blank') {
        route.continue();
        return;
      }
      route.abort();
    });
    await page.goto(inputUrl, { waitUntil: 'load' });
    await page.evaluate(async ({ viewportWidth, viewportHeight }) => {
      document.documentElement.style.width = `${viewportWidth}px`;
      document.documentElement.style.height = `${viewportHeight}px`;
      document.body.style.width = `${viewportWidth}px`;
      document.body.style.height = `${viewportHeight}px`;
      document.body.style.margin = '0';
      document.body.style.overflow = 'hidden';
      await document.fonts?.ready;
    }, { viewportWidth: width, viewportHeight: height });
    await page.screenshot({
      path: outputPath,
      clip: { x: 0, y: 0, width, height },
      omitBackground: false,
    });
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
