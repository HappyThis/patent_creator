import { promises as fs } from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const DEFAULT_DRAWIO_URL = 'https://embed.diagrams.net/?embed=1&proto=json&spin=1&ui=min&libraries=0&noExitBtn=1&noSaveBtn=1';

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key.startsWith('--') || value == null || value.startsWith('--')) {
      throw new Error('Usage: node render-figure-drawio.mjs --input diagram.drawio --output render.png --width 1500 --height 900');
    }
    args[key.slice(2)] = value;
    index += 1;
  }
  return args;
}

async function exportPng({ xml, width, height, drawioUrl }) {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      viewport: {
        width: Math.max(1200, width),
        height: Math.max(760, height),
      },
    });
    await page.setContent(
      `<!doctype html>
<html>
  <body style="margin:0">
    <iframe
      id="drawio-frame"
      title="draw.io renderer"
      src="${escapeHtml(drawioUrl)}"
      style="position:fixed;inset:0;width:100vw;height:100vh;border:0"
    ></iframe>
  </body>
</html>`,
      { waitUntil: 'domcontentloaded' },
    );

    return await page.evaluate(
      ({ xml, width, height }) =>
        new Promise((resolve, reject) => {
          const iframe = document.getElementById('drawio-frame');
          if (!(iframe instanceof HTMLIFrameElement) || iframe.contentWindow == null) {
            reject(new Error('draw.io iframe not available'));
            return;
          }

          const timer = window.setTimeout(() => {
            window.removeEventListener('message', handleMessage);
            reject(new Error('draw.io export timed out'));
          }, 45000);

          function cleanup() {
            window.clearTimeout(timer);
            window.removeEventListener('message', handleMessage);
          }

          function post(message) {
            iframe.contentWindow.postMessage(JSON.stringify(message), '*');
          }

          function handleMessage(event) {
            if (event.source !== iframe.contentWindow || !event.data) {
              return;
            }
            let message;
            try {
              message = JSON.parse(event.data);
            } catch {
              return;
            }
            if (message.error) {
              cleanup();
              reject(new Error(String(message.error)));
              return;
            }
            if (message.event === 'init') {
              post({
                action: 'load',
                autosave: 0,
                modified: '0',
                noExitBtn: 1,
                noSaveBtn: 1,
                saveAndExit: '0',
                title: 'figure.drawio',
                xml,
              });
              return;
            }
            if (message.event === 'load') {
              post({
                action: 'export',
                format: 'png',
                xml,
                width,
                height,
                border: 0,
                grid: false,
                shadow: false,
                transparent: false,
                background: '#ffffff',
                size: 'page',
              });
              return;
            }
            if (message.event === 'export') {
              const data = typeof message.data === 'string' ? message.data : '';
              if (!data.startsWith('data:image/png;base64,')) {
                cleanup();
                reject(new Error('draw.io did not return a PNG data URI'));
                return;
              }
              cleanup();
              resolve(data);
            }
          }

          window.addEventListener('message', handleMessage);
        }),
      { xml, width, height },
    );
  } finally {
    await browser.close();
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

async function main() {
  const args = parseArgs(process.argv);
  const inputPath = args.input;
  const outputPath = args.output;
  const width = Number(args.width || 1500);
  const height = Number(args.height || 900);
  const drawioUrl = args['drawio-url'] || process.env.DRAWIO_EMBED_URL || DEFAULT_DRAWIO_URL;
  if (!inputPath || !outputPath || !Number.isFinite(width) || !Number.isFinite(height)) {
    throw new Error('Usage: node render-figure-drawio.mjs --input diagram.drawio --output render.png --width 1500 --height 900');
  }
  const xml = await fs.readFile(inputPath, 'utf-8');
  const dataUri = await exportPng({ xml, width, height, drawioUrl });
  const base64 = dataUri.slice('data:image/png;base64,'.length);
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.writeFile(outputPath, Buffer.from(base64, 'base64'));
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
