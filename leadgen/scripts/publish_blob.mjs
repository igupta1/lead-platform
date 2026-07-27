// Publish the per-niche lead inventory to Vercel Blob using ONLY the blob
// read-write token — no `vercel login` / account token (the CLI needs one; the
// SDK does not). Run in CI after `python -m leadgen.run` writes leadgen/data/*.json.
//
//   npm i @vercel/blob@latest --no-save && node scripts/publish_blob.mjs
//
// Uploads each <niche>-leads.json + taxonomy.json to a STABLE public pathname
// (overwritten each run) with a short cache-control so the outreach engine reads
// same-day data. Prints each URL and, on the last line, `OUTREACH base: <origin>`.

import { readdir, readFile } from "node:fs/promises";

import { put } from "@vercel/blob";

const DATA_DIR = new URL("../data/", import.meta.url);
const TOKEN = process.env.BLOB_READ_WRITE_TOKEN;
const CACHE_MAX_AGE = 300; // seconds (Vercel minimum is 60)

if (!TOKEN) {
  console.error("BLOB_READ_WRITE_TOKEN unset — cannot publish");
  process.exit(1);
}

const names = (await readdir(DATA_DIR)).filter(
  (f) => f.endsWith("-leads.json") || f === "taxonomy.json",
);
if (names.length === 0) {
  console.error("no inventory files to publish in data/");
  process.exit(1);
}

let base = null;
for (const name of names) {
  const body = await readFile(new URL(name, DATA_DIR));
  const res = await put(name, body, {
    access: "public",
    addRandomSuffix: false, // stable pathname
    allowOverwrite: true, // republished every run
    contentType: "application/json",
    cacheControlMaxAge: CACHE_MAX_AGE,
    token: TOKEN,
  });
  console.log(`published ${name} -> ${res.url}`);
  if (!base) base = new URL(res.url).origin;
}
if (base) console.log(`OUTREACH base: ${base}`);
