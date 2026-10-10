// node drawer_extra_harness.js APP_JS CASES_JSON
// Runs app.js's extraLines() (the finding drawer's rendering of `extra`)
// on each case {key, value} with the given meta, and prints the lines.
"use strict";
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

// the source text of one top-level function / const of app.js
function grab(name) {
  let i = src.indexOf("function " + name + "(");
  if (i < 0) {
    i = src.indexOf("const " + name + " =");
    if (i < 0) throw new Error("missing " + name);
    return src.slice(i, src.indexOf(";", i) + 1);
  }
  let depth = 0;
  for (let k = src.indexOf("{", i); k < src.length; k++) {
    if (src[k] === "{") depth++;
    else if (src[k] === "}" && --depth === 0) return src.slice(i, k + 1);
  }
  throw new Error("unbalanced " + name);
}

const code = [
  "const S = { meta: data.meta };",
  ...["originInfo", "evLabel", "tanachSourceLabel", "editionRefText",
      "CTX_SCOPE_HE", "extraLines"].map(grab),
  "return data.cases.map((c) => extraLines(c.key, c.value));",
].join("\n");
process.stdout.write(JSON.stringify(new Function("data", code)(data)));
