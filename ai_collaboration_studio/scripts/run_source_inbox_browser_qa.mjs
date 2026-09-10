import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

function requiredEnv(name) {
  const value = String(process.env[name] || "").trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const baseUrl = new URL(requiredEnv("AI_STUDIO_QA_BASE_URL"));
if (!new Set(["127.0.0.1", "localhost", "[::1]", "::1"]).has(baseUrl.hostname)) {
  throw new Error("AI_STUDIO_QA_BASE_URL must use a loopback host");
}
if (!/^https?:$/.test(baseUrl.protocol)) {
  throw new Error("AI_STUDIO_QA_BASE_URL must use HTTP(S)");
}

const outputDir = path.resolve(requiredEnv("AI_STUDIO_QA_OUTPUT_DIR"));
const playwrightPath = requiredEnv("AI_STUDIO_PLAYWRIGHT_PATH");
const executablePath = path.resolve(requiredEnv("AI_STUDIO_BROWSER_EXECUTABLE"));
const { chromium } = require(playwrightPath);

await fs.mkdir(outputDir, { recursive: true });

const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ["--disable-background-networking", "--disable-component-update"],
});

const consoleErrors = [];
const pageErrors = [];
const unexpectedRequests = [];
let page;

try {
  const context = await browser.newContext({
    locale: "zh-CN",
    viewport: { width: 1536, height: 960 },
    reducedMotion: "reduce",
  });
  page = await context.newPage();
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(String(error?.stack || error)));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin !== baseUrl.origin) unexpectedRequests.push(request.url());
  });

  await page.goto(baseUrl.href, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: /^来源收件箱/ }).click();
  const panel = page.locator(".source-inbox-panel.open");
  await panel.waitFor({ state: "visible" });
  await page.getByText("external_unverified", { exact: true }).first().waitFor();

  const initialText = await panel.innerText();
  for (const expected of [
    "外部信息默认不可信",
    "已阅，不代表事实确认",
    "目标房间（手动选择）",
    "草稿不启动 Provider",
    "工作状态（全局计数）",
    "来源与阅读状态（全局计数）",
    "确定性研究影响映射",
    "不是方向预测、因果结论、盈利声明或执行授权",
  ]) {
    if (!initialText.includes(expected)) throw new Error(`missing boundary copy: ${expected}`);
  }
  if (await panel.locator(".source-inbox-room-actions select").inputValue() !== "") {
    throw new Error("room target was preselected");
  }
  await panel.locator(".source-inbox-health summary").click();
  await panel.getByText(/运行时在线性未核验/).waitFor();
  await panel.getByRole("button", { name: "启用通知", exact: true }).waitFor();
  const firstRow = panel.locator(".source-inbox-list-items .source-inbox-row").first();
  await firstRow.click();
  const selectedItemId = new URL(page.url()).searchParams.get("source_event");
  if (!selectedItemId) throw new Error("selected Source Inbox event did not create a deep link");

  await page.screenshot({
    path: path.join(outputDir, "source-inbox-desktop-before.png"),
    fullPage: true,
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: path.join(outputDir, "source-inbox-mobile-before.png"),
    fullPage: true,
  });

  await page.setViewportSize({ width: 1536, height: 960 });
  const acknowledgement = panel.locator('.source-inbox-acknowledgement input[type="checkbox"]');
  await acknowledgement.check();
  await panel.getByRole("button", { name: "记录已阅", exact: true }).click();
  await panel.getByText("已记录为已阅；这不代表事实确认。", { exact: true }).waitFor();

  const roomSelect = panel.locator(".source-inbox-room-actions select");
  const roomValue = await roomSelect.locator("option:not([value=''])").first().getAttribute("value");
  if (!roomValue) throw new Error("no room option available");
  await roomSelect.selectOption(roomValue);
  await panel.getByRole("button", { name: "附加到房间", exact: true }).click();
  await panel.getByText(/来源已作为未核验材料附加到所选房间/).waitFor();

  await panel.locator(".source-inbox-draft-actions textarea").fill(
    "核对 GitHub / CI 来源中的失败断言、反证与未知项；只形成研究草稿。",
  );
  await panel.getByRole("button", { name: "仅生成 round draft", exact: true }).click();
  await panel.getByText(
    "仅生成了 round draft；Provider、正式 round 与市场调用均未启动。",
    { exact: true },
  ).waitFor();

  await page.screenshot({
    path: path.join(outputDir, "source-inbox-desktop-drafted.png"),
    fullPage: true,
  });

  if (consoleErrors.length || pageErrors.length || unexpectedRequests.length) {
    throw new Error(JSON.stringify({ consoleErrors, pageErrors, unexpectedRequests }));
  }

  process.stdout.write(`${JSON.stringify({
    ok: true,
    base_url: baseUrl.origin,
    screenshots: [
      "source-inbox-desktop-before.png",
      "source-inbox-mobile-before.png",
      "source-inbox-desktop-drafted.png",
    ],
    console_errors: 0,
    page_errors: 0,
    unexpected_network_requests: 0,
  })}\n`);
} finally {
  await browser.close();
}
