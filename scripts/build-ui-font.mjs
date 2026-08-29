import { spawn } from "node:child_process";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourceFont = process.env.XIANYU_SAAS_FONT_SOURCE || path.join(root, "vendor", "fonts", "NotoSansSC-Variable.ttf");
const outputFont = path.join(root, "frontend", "assets", "ui-sans-generated.woff2");
const sourceFiles = [
  "frontend/index.html",
  "frontend/assets/app.js",
  "backend/app.py",
  "backend/access.py",
];
const candidates = [
  process.env.XIANYU_SAAS_PYFTSUBSET,
  "pyftsubset",
].filter(Boolean);

async function executableExists(command) {
  if (command.includes(path.sep)) {
    try { await access(command); return true; } catch { return false; }
  }
  return new Promise((resolve) => {
    const child = spawn("sh", ["-lc", `command -v ${command}`], { stdio: "ignore" });
    child.once("exit", (code) => resolve(code === 0));
  });
}

let tool = "";
for (const candidate of candidates) {
  if (await executableExists(candidate)) { tool = candidate; break; }
}
if (!tool) throw new Error("pyftsubset is required; set XIANYU_SAAS_PYFTSUBSET");

const ascii = ` !"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_abcdefghijklmnopqrstuvwxyz{|}~`;
const gb2312 = [];
const gb2312Decoder = new TextDecoder("gb18030", { fatal: true });
for (const row of [...Array.from({ length: 9 }, (_, index) => index + 1), ...Array.from({ length: 72 }, (_, index) => index + 16)]) {
  for (let column = 1; column <= 94; column += 1) {
    try {
      const decoded = gb2312Decoder.decode(Uint8Array.from([row + 0xA0, column + 0xA0]));
      if (decoded && decoded !== "�") gb2312.push(decoded);
    } catch {
      // Unassigned GB2312 positions are intentionally skipped.
    }
  }
}
const sourceText = (await Promise.all(sourceFiles.map((file) => readFile(path.join(root, file), "utf8")))).join("\n");
const characters = [...new Set(ascii + gb2312.join("") + sourceText)].sort((left, right) => left.codePointAt(0) - right.codePointAt(0)).join("");
const temporaryDirectory = await mkdtemp(path.join(tmpdir(), "xianyu-saas-font-"));
const textFile = path.join(temporaryDirectory, "characters.txt");

try {
  await writeFile(textFile, characters, "utf8");
  await new Promise((resolve, reject) => {
    const child = spawn(tool, [
      sourceFont,
      `--text-file=${textFile}`,
      `--output-file=${outputFont}`,
      "--flavor=woff2",
      "--layout-features=*",
      "--name-IDs=*",
      "--name-legacy",
      "--name-languages=*",
    ], { stdio: "inherit" });
    child.once("error", reject);
    child.once("exit", (code) => code === 0 ? resolve() : reject(new Error(`pyftsubset exited with ${code}`)));
  });
} finally {
  await rm(temporaryDirectory, { recursive: true, force: true });
}

process.stdout.write(`UI font rebuilt with ${[...characters].length} characters: ${outputFont}\n`);
