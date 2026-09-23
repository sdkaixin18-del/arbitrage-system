"use strict";

const fs = require("fs");
const path = require("path");

const outputPath = process.env.ASTRO_CHAIN_LABEL_FEED
  || "/home/ubuntu/astro-admin/dist/auto-chain-labels.json";

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", async () => {
  const update = JSON.parse(input || "{}");
  const id = String(update.id || "").trim();
  const text = String(update.text || "").trim();
  if (!id || !text) throw new Error("chain label update requires id and text");

  // Independent card publishers can finish at the same time. Serialize the
  // read/modify/write so one card cannot overwrite another card's label.
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  const lockPath = `${outputPath}.lock`;
  let lock;
  for (let attempt = 0; attempt < 200; attempt += 1) {
    try {
      lock = fs.openSync(lockPath, "wx", 0o600);
      break;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      // Recover a lock left by a terminated publisher, never a live short write.
      try {
        if (Date.now() - fs.statSync(lockPath).mtimeMs > 30000) fs.unlinkSync(lockPath);
      } catch (statError) { if (statError.code !== "ENOENT") throw statError; }
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }
  if (lock === undefined) throw new Error("chain label feed is busy");
  try {
    let document = { version: 1, updatedAt: 0, items: {} };
    try {
      const current = JSON.parse(fs.readFileSync(outputPath, "utf8"));
      if (current && typeof current === "object") document = current;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (!document.items || typeof document.items !== "object") document.items = {};
    document.version = 1;
    document.updatedAt = Date.now();
    document.items[id] = {
      text,
      symbol: String(update.symbol || "").trim(),
      chainIndex: String(update.chainIndex || "").trim(),
      contractAddress: String(update.contractAddress || "").trim(),
      updatedAt: Number(update.updatedAt) || Date.now(),
      // An explicit missing-label repair may retry an already-applied card once.
      // Ordinary publications do not restore labels the user later cleared.
      ...(update.repairRevision ? { repairRevision: String(update.repairRevision) }
          : document.items[id]?.repairRevision ? { repairRevision: document.items[id].repairRevision } : {}),
    };

    const temporary = `${outputPath}.tmp-${process.pid}`;
    fs.writeFileSync(temporary, `${JSON.stringify(document, null, 2)}\n`, { mode: 0o644 });
    fs.renameSync(temporary, outputPath);
    // Astro upgrades replace index.html. Repair the display hook on every
    // publication, within this same SSH request and without restarting Astro.
    const indexPath = path.join(path.dirname(outputPath), "index.html");
    const bridgePath = path.join(path.dirname(outputPath), "auto-chain-labels.js");
    if (!fs.existsSync(bridgePath)) throw new Error("chain label display script is missing");
    const html = fs.readFileSync(indexPath, "utf8");
    let repaired = false;
    if (!/<script\b[^>]*\bsrc=["'][^"']*auto-chain-labels\.js(?:\?[^"']*)?["']/i.test(html)) {
      if (!/<\/head>/i.test(html)) throw new Error("Astro index has no head element");
      const tag = '<script defer src="./auto-chain-labels.js"></script>';
      const next = html.replace(/<\/head>/i, `${tag}</head>`);
      const indexTemporary = `${indexPath}.tmp-${process.pid}`;
      fs.writeFileSync(indexTemporary, next, { mode: 0o644 });
      fs.renameSync(indexTemporary, indexPath);
      repaired = true;
    }
    process.stdout.write(JSON.stringify({ ok: true, id, text, displayHookReady: true, displayHookRepaired: repaired }));
  } finally {
    fs.closeSync(lock);
    fs.unlinkSync(lockPath);
  }
});
