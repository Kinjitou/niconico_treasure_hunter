"""埋もれた良作発掘くん — Snapshot API から候補プールを取得して JSON に保存する。

標準ライブラリのみで動く。GitHub Actions から毎朝実行する想定。
規約上の配慮：投稿者ID・チャンネルID・最新コメントは取得も保存もしない。
説明文は「雰囲気で探す」用の埋め込み計算にだけ使い、リポジトリには保存しない（build/ は .gitignore 対象）。
"""

from __future__ import annotations

import datetime as dt
import gzip
import html
import json
import os
import pathlib
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
API = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/video/contents/search"
VERSION_API = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/version"
JST = dt.timezone(dt.timedelta(hours=9))
FIELDS = [
    "contentId", "title", "thumbnailUrl", "viewCounter", "likeCounter",
    "mylistCounter", "commentCounter", "lengthSeconds", "startTime",
    "genre", "tags", "contentType", "description",
]
LATEST = ROOT / "docs" / "data" / "latest.json"
HISTORY_DIR = ROOT / "data" / "history"
BUILD_DIR = ROOT / "build"


def log(msg: str) -> None:
    print(msg, flush=True)


class Client:
    def __init__(self, context: str, repo_url: str):
        self.context = context
        self.ua = f"{context} (+{repo_url})"
        self.last_elapsed = 0.0
        self.requests = 0

    def get_json(self, url: str) -> dict:
        # 利用制限：前回のレスポンス時間と同じだけ待つ（最低1秒）
        time.sleep(max(1.0, self.last_elapsed))
        for attempt in range(4):
            req = urllib.request.Request(url, headers={"User-Agent": self.ua})
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=60) as res:
                    body = json.load(res)
                self.last_elapsed = time.monotonic() - started
                self.requests += 1
                return body
            except urllib.error.HTTPError as e:
                self.last_elapsed = time.monotonic() - started
                if e.code == 503:
                    # ドキュメント：503 は5分以上あけてリトライ
                    wait = 330
                elif e.code >= 500:
                    wait = 15 * (attempt + 1)
                else:
                    detail = e.read().decode("utf-8", "replace")[:500]
                    raise RuntimeError(f"HTTP {e.code}: {detail}") from e
                log(f"  HTTP {e.code}、{wait}秒待ってリトライ（{attempt + 1}/3）")
                time.sleep(wait)
            except urllib.error.URLError as e:
                log(f"  通信エラー {e.reason}、20秒待ってリトライ（{attempt + 1}/3）")
                time.sleep(20)
        raise RuntimeError("リトライ上限に達した")

    def search(self, params: list[tuple[str, str]]) -> dict:
        params = params + [("_context", self.context)]
        url = API + "?" + urllib.parse.urlencode(params)
        body = self.get_json(url)
        status = body.get("meta", {}).get("status")
        if status != 200:
            raise RuntimeError(f"API error: {body.get('meta')}")
        return body


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def clean_description(raw: str | None, limit: int = 400) -> str:
    text = re.sub(r"<br\s*/?>", " ", raw or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b(?:sm|so|nm|mylist/|user/)\d+\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def compact(v: dict) -> dict:
    tags = (v.get("tags") or "").split()
    return {
        "id": v["contentId"],
        "title": v.get("title") or "",
        "thumb": v.get("thumbnailUrl") or "",
        "views": v.get("viewCounter") or 0,
        "likes": v.get("likeCounter") or 0,
        "mylists": v.get("mylistCounter") or 0,
        "comments": v.get("commentCounter") or 0,
        "length": v.get("lengthSeconds") or 0,
        "posted": v.get("startTime") or "",
        "genre": v.get("genre") or "",
        "tags": tags[:12],
        "type": v.get("contentType") or "",
    }


def main() -> int:
    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    pool = cfg["pool"]
    repo_url = "https://github.com/" + os.environ.get("GITHUB_REPOSITORY", "local/run")
    client = Client(cfg["api"]["context"], repo_url)
    force = "--force" in sys.argv

    version = client.get_json(VERSION_API)
    last_modified = version["last_modified"]
    log(f"スナップショット切り替え日時: {last_modified}")

    if LATEST.exists() and not force:
        prev = json.loads(LATEST.read_text(encoding="utf-8"))
        if prev.get("meta", {}).get("snapshot") == last_modified:
            log("前回と同じスナップショットなので何もしない")
            return 0

    # データは AM5:00 時点。その日の 5:00 JST を基準に「投稿後N日以内」を切る
    lm = dt.datetime.fromisoformat(last_modified).astimezone(JST)
    ref = lm.replace(hour=5, minute=0, second=0, microsecond=0)
    if lm < ref:
        ref -= dt.timedelta(days=1)
    days = int(pool["window_days"])
    start = ref - dt.timedelta(days=days)

    base: list[tuple[str, str]] = [
        ("q", ""),
        ("fields", ",".join(FIELDS)),
        ("_sort", "-likeCounter"),
        ("_limit", "100"),
        ("filters[viewCounter][gte]", str(pool["min_views"])),
        ("filters[viewCounter][lte]", str(pool["max_views"])),
        ("filters[likeCounter][gte]", str(pool["min_likes"])),
    ]
    if pool.get("content_type"):
        base.append(("filters[contentType][0]", pool["content_type"]))

    items: dict[str, dict] = {}
    descriptions: dict[str, str] = {}
    slices = []
    for i in range(days):
        s = start + dt.timedelta(days=i)
        e = s + dt.timedelta(days=1)
        params = base + [
            ("filters[startTime][gte]", iso(s)),
            ("filters[startTime][lt]", iso(e)),
        ]
        total = None
        fetched = 0
        offset = 0
        while offset < int(pool["per_day_max"]):
            body = client.search(params + [("_offset", str(offset))])
            total = body["meta"]["totalCount"]
            data = body["data"]
            for v in data:
                items[v["contentId"]] = compact(v)
                descriptions[v["contentId"]] = clean_description(v.get("description"))
            fetched += len(data)
            if len(data) < 100:
                break
            offset += 100
        log(f"  {s:%m/%d}投稿: 条件一致 {total}件 / 取得 {fetched}件")
        slices.append({"from": iso(s), "to": iso(e), "total": total, "fetched": fetched})

    videos = sorted(items.values(), key=lambda v: v["posted"], reverse=True)
    out = {
        "meta": {
            "snapshot": last_modified,
            "reference": iso(ref),
            "generated_at": iso(dt.datetime.now(JST)),
            "pool": pool,
            "slices": slices,
            "count": len(videos),
            "requests": client.requests,
        },
        "videos": videos,
    }

    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(HISTORY_DIR / f"{ref:%Y-%m-%d}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    BUILD_DIR.mkdir(exist_ok=True)
    (BUILD_DIR / "descriptions.json").write_text(
        json.dumps(descriptions, ensure_ascii=False), encoding="utf-8")
    (BUILD_DIR / "updated").write_text(last_modified, encoding="utf-8")
    log(f"保存: {len(videos)}件（APIリクエスト {client.requests}回）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
