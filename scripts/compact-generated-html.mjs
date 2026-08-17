#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const target = path.resolve(process.argv[2] ?? "index.html");
let html = fs.readFileSync(target, "utf8");

// index.html is generated from readable scripts + ledger.json. Compact only the
// repetitive generated sections so the framework's file-size lint remains useful.
html = html.replace(
  /<div id="el-([^"]+)" class="scene-wrapper"([^>]*)>\s*<video([^>]*)><\/video>\s*<\/div>/g,
  '<div id="el-$1" class="scene-wrapper"$2><video$3></video></div>',
);
html = html.replace(/^\s*\/\/ SEAM —.*\r?\n/gm, "");
html = html.replace(/^\s*\r?\n/gm, "");
fs.writeFileSync(target, html);
console.log(`compacted generated HTML: ${target} (${html.split(/\r?\n/).length} lines)`);

