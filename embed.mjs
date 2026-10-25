// 埋もれた良作発掘くん — 「雰囲気で探す」用の埋め込みを作る
// 入力: docs/data/latest.json（動画一覧）と build/descriptions.json（説明文。リポジトリには保存しない）
// 出力: docs/data/emb.bin（int8 × 次元数 × 本数、latest.json と同じ並び）と docs/data/emb.json（メタ情報）
// ページ側も同じモデル・同じ量子化（q8）で検索語をベクトル化するので、ここを変えたら index.html も合わせること。

import { readFile, writeFile } from "node:fs/promises";
import { pipeline, env } from "@huggingface/transformers";

const MODEL = "Xenova/multilingual-e5-small";
const DTYPE = "q8";
const BATCH = 32;
const MAX_CHARS = 500;

env.cacheDir = "./.hf-cache";

const latest = JSON.parse(await readFile("docs/data/latest.json", "utf8"));
const descriptions = JSON.parse(await readFile("build/descriptions.json", "utf8"));
const videos = latest.videos;

// e5 系モデルは文書側に "passage: "、検索語側に "query: " を付ける決まり
const texts = videos.map((v) => {
  const body = [v.title, v.tags.join(" "), descriptions[v.id] || ""].join("\n");
  return "passage: " + body.slice(0, MAX_CHARS);
});

const started = Date.now();
const extractor = await pipeline("feature-extraction", MODEL, { dtype: DTYPE });
console.log(`モデル読み込み: ${((Date.now() - started) / 1000).toFixed(1)}秒`);

let dim = 0;
let out = null;
for (let i = 0; i < texts.length; i += BATCH) {
  const batch = texts.slice(i, i + BATCH);
  const tensor = await extractor(batch, { pooling: "mean", normalize: true });
  if (!out) {
    dim = tensor.dims[1];
    out = new Int8Array(texts.length * dim);
  }
  const data = tensor.data;
  for (let j = 0; j < batch.length; j++) {
    for (let k = 0; k < dim; k++) {
      const x = data[j * dim + k];
      out[(i + j) * dim + k] = Math.max(-127, Math.min(127, Math.round(x * 127)));
    }
  }
  if ((i / BATCH) % 20 === 0) {
    console.log(`  ${Math.min(i + BATCH, texts.length)} / ${texts.length}`);
  }
}

await writeFile("docs/data/emb.bin", Buffer.from(out.buffer));
await writeFile(
  "docs/data/emb.json",
  JSON.stringify({
    model: MODEL,
    dtype: DTYPE,
    dim,
    count: videos.length,
    snapshot: latest.meta.snapshot,
    scale: 127,
  }),
);
console.log(`埋め込み完了: ${videos.length}本 × ${dim}次元、${((Date.now() - started) / 1000).toFixed(1)}秒`);
