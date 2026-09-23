/**
 * Records a product-demo video of the Heimdall dashboard.
 *
 * Three scenes: a prompt injection blocked at the gate, a trivial prompt
 * cleared and routed to the cheap model, then the live traffic log.
 *
 * Both servers must already be running:
 *   backend   http://localhost:8000
 *   frontend  http://localhost:5173
 *
 * Usage:  node tools/record-demo.mjs
 * Output: tools/out/heimdall-demo.webm  (convert to mp4 with ffmpeg)
 */

import { chromium } from "playwright";
import { mkdirSync, readdirSync, renameSync, rmSync } from "node:fs";
import { join } from "node:path";

const APP = process.env.APP_URL ?? "http://localhost:5173";
const OUT = join(import.meta.dirname, "out");
const W = 1280;
const H = 720;

/** Ease the page to an absolute scroll position and let it settle. */
async function scrollTo(page, y, settle = 1100) {
  await page.evaluate((top) => window.scrollTo({ top, behavior: "smooth" }), y);
  await page.waitForTimeout(settle);
}

/** Centre an element in the viewport, smoothly. */
async function scrollToEl(page, selector, block = "center", settle = 1100) {
  await page.evaluate(
    ([sel, blk]) => document.querySelector(sel)?.scrollIntoView({ behavior: "smooth", block: blk }),
    [selector, block],
  );
  await page.waitForTimeout(settle);
}

/** Run one demo prompt and hold on its verdict. */
async function runPrompt(page, chipLabel, expectStamp, holdMs) {
  // Bring the chip row into view before clicking it.
  await scrollToEl(page, ".composer", "center", 900);
  // Match on text content, not the accessible name: the chips carry a CSS
  // ::before bullet that gets folded into the accessible name.
  await page.locator("button.chip", { hasText: chipLabel }).first().click();

  // Frame the pipeline while the packet is still crossing.
  await scrollToEl(page, ".pipeline", "center", 400);

  // Wait for the real verdict rather than a fixed sleep.
  await page.waitForSelector(`.stamp.${expectStamp}`, { timeout: 60_000 });
  await page.waitForTimeout(holdMs);
}

async function main() {
  rmSync(OUT, { recursive: true, force: true });
  mkdirSync(OUT, { recursive: true });

  // Use the Chrome already installed on this machine rather than Playwright's
  // bundled Chromium, whose download is blocked here.
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: W, height: H },
    deviceScaleFactor: 2,          // render at 2x so downsampled text stays crisp
    reducedMotion: "no-preference", // the animation is the point
    recordVideo: { dir: OUT, size: { width: W, height: H } },
  });

  const page = await context.newPage();
  await page.goto(APP, { waitUntil: "networkidle" });

  // The dashboard disables input until the model has loaded.
  console.log("waiting for the model...");
  await page.waitForSelector('.status:has-text("Laya ready")', { timeout: 180_000 });
  await page.waitForTimeout(1600); // hold on the masthead + metrics

  // --- Scene 1: an attack is stopped at the gate ------------------------
  console.log("scene 1: blocked");
  await runPrompt(page, "Classic injection", "blocked", 2600);
  // Drop to the firewall readouts so the reasons are legible.
  await scrollToEl(page, ".readout", "center", 1200);
  await page.waitForTimeout(2200);

  // --- Scene 2: a trivial prompt clears and routes cheap ----------------
  console.log("scene 2: routed");
  await runPrompt(page, "Trivial lookup", "clean", 2800);
  await scrollToEl(page, ".readout", "center", 1200);
  await page.waitForTimeout(2000);

  // --- Scene 3: the live traffic log ------------------------------------
  console.log("scene 3: traffic");
  await scrollToEl(page, ".traffic", "start", 1400);
  await page.waitForTimeout(1800);
  // Ease down the log, then settle on the footer.
  const bottom = await page.evaluate(() => document.body.scrollHeight);
  await scrollTo(page, bottom, 2200);
  await page.waitForTimeout(1200);

  await context.close();   // finalises the video file
  await browser.close();

  const file = readdirSync(OUT).find((f) => f.endsWith(".webm"));
  if (!file) throw new Error("playwright produced no video");
  renameSync(join(OUT, file), join(OUT, "heimdall-demo.webm"));
  console.log("wrote", join(OUT, "heimdall-demo.webm"));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
