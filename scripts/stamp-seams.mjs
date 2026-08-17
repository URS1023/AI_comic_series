#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

const root = path.resolve(process.argv[2] ?? ".");
const candidates = [
  process.env.MOTION_DOCTRINE_SKILL_DIR
    ? path.join(process.env.MOTION_DOCTRINE_SKILL_DIR, "scripts", "seam-stamp.mjs")
    : null,
  path.join(os.homedir(), ".agents", "skills", "motion-doctrine", "scripts", "seam-stamp.mjs"),
  path.join(os.homedir(), ".codex", "skills", "motion-doctrine", "scripts", "seam-stamp.mjs"),
].filter(Boolean);
const script = candidates.find((candidate) => fs.existsSync(candidate));
if (!script) {
  throw new Error(
    "motion-doctrine seam-stamp.mjs is unavailable. Install the skill or set MOTION_DOCTRINE_SKILL_DIR.",
  );
}
const result = spawnSync(
  process.execPath,
  [script, "--ledger", path.join(root, "ledger.json"), "--write", path.join(root, "index.html")],
  { cwd: root, encoding: "utf8", stdio: "inherit" },
);
if (result.status !== 0) process.exit(result.status ?? 1);

