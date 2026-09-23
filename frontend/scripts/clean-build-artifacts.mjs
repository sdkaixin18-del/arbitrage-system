import { readdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const distRoot = fileURLToPath(new URL("../dist/", import.meta.url));

async function removeMacMetadata(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  await Promise.all(
    entries.map(async (entry) => {
      const target = join(directory, entry.name);
      if (entry.name === ".DS_Store" || entry.name.startsWith("._")) {
        await rm(target, { force: true, recursive: entry.isDirectory() });
        return;
      }
      if (entry.isDirectory()) await removeMacMetadata(target);
    })
  );
}

await removeMacMetadata(distRoot);
