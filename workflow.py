#!/usr/bin/env python3
"""
SCP API → Markdown + Russian translation workflow.

Fetches data from https://github.com/scp-data/scp-api via GitHub API,
converts JSON entries to Markdown, translates to Russian with argostranslate,
and writes output to ./output/.

Usage:
    python workflow.py [--section SECTION] [--limit N] [--no-translate]
                       [--github-token TOKEN] [--workers N]

Sections: items, tales, hubs, goi  (default: all)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import requests
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO = "scp-data/scp-api"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/main/docs/data/scp"
API_BASE = "https://api.github.com"

SECTIONS = {
    "items": {
        "index": f"{RAW_BASE}/items/index.json",
        "content_index": f"{RAW_BASE}/items/content_index.json",
        "content_base": f"{RAW_BASE}/items/",
        "label": "Объект СЦП",
    },
    "tales": {
        "index": f"{RAW_BASE}/tales/index.json",
        "label": "Рассказ",
    },
    "hubs": {
        "index": f"{RAW_BASE}/hubs/index.json",
        "label": "Хаб",
    },
    "goi": {
        "index": f"{RAW_BASE}/goi/index.json",
        "label": "Группа Интересов",
    },
}

OUTPUT_DIR = Path("output")
CACHE_DIR = Path("cache")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# Fields to translate (text-heavy, skip links/dates/ids)
TRANSLATE_FIELDS = {"title", "raw_content"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("scp-workflow")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class GithubClient:
    """Thin wrapper around requests with rate-limit awareness."""

    def __init__(self, token: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "User-Agent": "scp-md-ru-workflow/1.0",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get_json(self, url: str, **params) -> Any:
        """GET JSON with automatic retry on rate-limit (429 / 403)."""
        for attempt in range(5):
            resp = self.session.get(url, params=params, timeout=60)
            if resp.status_code in (429, 403):
                reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset - int(time.time()), 5)
                log.warning("Rate limited — sleeping %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Failed to fetch {url} after retries")

    def download_raw(self, url: str, dest: Path) -> Path:
        """Download a potentially large raw file with streaming."""
        if dest.exists():
            log.debug("Cache hit: %s", dest.name)
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        log.info("Downloading %s …", url.split("/")[-1])
        with self.session.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            with open(tmp, "wb") as fh, tqdm(
                total=total, unit="B", unit_scale=True,
                desc=dest.name, leave=False
            ) as bar:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    fh.write(chunk)
                    bar.update(len(chunk))
        tmp.rename(dest)
        return dest

    def list_repo_tree(self, repo: str, branch: str = "main") -> list[dict]:
        """Return flat list of all blobs in the repo tree (avoids recursive API limits)."""
        data = self.get_json(
            f"{API_BASE}/repos/{repo}/git/trees/{branch}",
            recursive=1,
        )
        return data.get("tree", [])


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

_translator = None


def _init_translator():
    """Lazy-init argostranslate en→ru, downloading the package if needed."""
    global _translator
    if _translator is not None:
        return _translator

    try:
        import argostranslate.package
        import argostranslate.translate
    except ImportError:
        log.error("argostranslate not installed — pip install argostranslate")
        return None

    from_code, to_code = "en", "ru"

    # Check if the package is already installed
    installed = argostranslate.package.get_installed_packages()
    pkg = next(
        (p for p in installed if p.from_code == from_code and p.to_code == to_code),
        None,
    )

    if pkg is None:
        log.info("Downloading argostranslate en→ru package …")
        argostranslate.package.update_package_index()
        available = argostranslate.package.get_available_packages()
        target = next(
            (p for p in available if p.from_code == from_code and p.to_code == to_code),
            None,
        )
        if target is None:
            log.error("en→ru argostranslate package not found in index")
            return None
        argostranslate.package.install_from_path(target.download())
        log.info("argostranslate en→ru installed.")

    _translator = argostranslate.translate.get_translation_from_codes(from_code, to_code)
    return _translator


def translate_text(text: str) -> str:
    """Translate a text block en→ru, returning original on failure."""
    if not text or not text.strip():
        return text
    tr = _init_translator()
    if tr is None:
        return text

    # Split into paragraphs to avoid hitting token limits per call
    paragraphs = text.split("\n")
    translated = []
    for para in paragraphs:
        if para.strip():
            try:
                translated.append(tr.translate(para))
            except Exception as exc:
                log.debug("Translation error: %s", exc)
                translated.append(para)
        else:
            translated.append(para)
    return "\n".join(translated)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _safe_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    return str(val)


def _clean_html(html: str) -> str:
    """Strip HTML tags, preserve newlines from <br> / <p> / <div>."""
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p>|</div>|</li>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", "", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def item_to_markdown(
    link: str,
    data: dict,
    section_label: str,
    do_translate: bool,
) -> str:
    """Convert a single JSON entry to a Markdown document."""

    title = _safe_str(data.get("title") or data.get("scp") or link)
    scp_num = data.get("scp_number")
    rating = data.get("rating")
    tags = data.get("tags", [])
    url = data.get("url", "")
    created_at = data.get("created_at", "")
    created_by = data.get("created_by", "")
    series = data.get("series", "")
    images = data.get("images", [])

    raw_content = _clean_html(_safe_str(data.get("raw_content", "")))

    if do_translate:
        title_ru = translate_text(title)
        content_ru = translate_text(raw_content) if raw_content else ""
    else:
        title_ru = title
        content_ru = raw_content

    lines: list[str] = []

    # Header
    lines.append(f"# {title_ru}")
    lines.append("")

    # Meta table
    lines.append("| Поле | Значение |")
    lines.append("|------|----------|")
    lines.append(f"| **Тип** | {section_label} |")
    if scp_num:
        lines.append(f"| **Номер** | {scp_num} |")
    if series:
        lines.append(f"| **Серия** | {series} |")
    if rating is not None:
        lines.append(f"| **Рейтинг** | {rating} |")
    if created_at:
        lines.append(f"| **Создано** | {created_at[:10]} |")
    if created_by:
        lines.append(f"| **Автор** | {created_by} |")
    if url:
        lines.append(f"| **Оригинал** | [{url}]({url}) |")
    lines.append("")

    # Tags
    if tags:
        tag_str = " ".join(f"`{t}`" for t in tags)
        lines.append(f"**Теги:** {tag_str}")
        lines.append("")

    # Images
    if images:
        lines.append("## Изображения")
        lines.append("")
        for img in images[:5]:  # cap at 5 to keep docs lean
            lines.append(f"![]({img})")
        lines.append("")

    # Content
    if content_ru:
        lines.append("## Содержание")
        lines.append("")
        lines.append(content_ru)
        lines.append("")

    # License footer
    lines.append("---")
    lines.append(
        "*Это переведённая копия материала с [SCP Wiki](http://www.scp-wiki.net/). "
        "Распространяется на условиях [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/).*"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

def write_markdown(out_dir: Path, slug: str, content: str) -> None:
    filename = re.sub(r"[^\w\-]", "_", slug) + ".md"
    path = out_dir / filename
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Section processing
# ---------------------------------------------------------------------------

def process_section(
    section: str,
    cfg: dict,
    client: GithubClient,
    do_translate: bool,
    limit: Optional[int],
    workers: int,
) -> int:
    """Download, convert, translate, and write all entries for a section."""

    out_dir = OUTPUT_DIR / section
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = CACHE_DIR / section
    cache_dir.mkdir(parents=True, exist_ok=True)

    label = cfg["label"]
    index_url = cfg["index"]
    index_cache = cache_dir / "index.json"

    # Download index
    client.download_raw(index_url, index_cache)
    log.info("[%s] Parsing index …", section)
    with open(index_cache, encoding="utf-8") as fh:
        index: dict = json.load(fh)

    # For items: merge content from content files into index entries
    if section == "items" and "content_index" in cfg:
        content_index_cache = cache_dir / "content_index.json"
        client.download_raw(cfg["content_index"], content_index_cache)
        with open(content_index_cache, encoding="utf-8") as fh:
            content_index: dict = json.load(fh)

        content_map: dict[str, dict] = {}
        for series_name, filename in content_index.items():
            content_url = cfg["content_base"] + filename
            content_cache = cache_dir / filename
            client.download_raw(content_url, content_cache)
            log.info("[%s] Parsing content file: %s …", section, filename)
            with open(content_cache, encoding="utf-8") as fh:
                series_data: dict = json.load(fh)
            content_map.update(series_data)

        # Merge raw_content into index entries
        for link, entry in index.items():
            if link in content_map:
                entry["raw_content"] = content_map[link].get("raw_content", "")

    entries = list(index.items())
    if limit:
        entries = entries[:limit]

    log.info("[%s] Processing %d entries …", section, len(entries))

    def _process(args: tuple) -> tuple[str, bool]:
        link, data = args
        try:
            md = item_to_markdown(link, data, label, do_translate)
            write_markdown(out_dir, link, md)
            return link, True
        except Exception as exc:
            log.warning("[%s] Failed %s: %s", section, link, exc)
            return link, False

    success = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process, e): e[0] for e in entries}
        with tqdm(total=len(entries), desc=f"{section}", unit="art") as bar:
            for fut in as_completed(futures):
                _, ok = fut.result()
                if ok:
                    success += 1
                bar.update(1)

    log.info("[%s] Done: %d/%d written", section, success, len(entries))
    return success


# ---------------------------------------------------------------------------
# README generation
# ---------------------------------------------------------------------------

def write_readme(sections_done: dict[str, int]) -> None:
    rows = "\n".join(
        f"| [{s}](output/{s}/) | {count} файлов |"
        for s, count in sections_done.items()
    )

    content = f"""\
# SCP API — Markdown / Русский

Автоматически сгенерированный датасет материалов [SCP Wiki](http://www.scp-wiki.net/)
в формате Markdown с переводом на русский язык через [argostranslate](https://github.com/argosopentech/argostranslate).

**Исходные данные:** [scp-data/scp-api](https://github.com/scp-data/scp-api) — обновляется ежедневно.

## Структура

| Раздел | Количество |
|--------|-----------|
{rows}

## Использование

```bash
# Установить зависимости
pip install -r requirements.txt

# Запустить полный workflow (все разделы)
python workflow.py

# Только SCP-объекты, первые 100, без перевода
python workflow.py --section items --limit 100 --no-translate

# С GitHub токеном (увеличивает лимит API до 5000 req/h)
python workflow.py --github-token ghp_xxxx
```

## Опции

| Флаг | Описание |
|------|----------|
| `--section` | Один из: `items`, `tales`, `hubs`, `goi` (по умолчанию — все) |
| `--limit N` | Ограничить количество статей |
| `--no-translate` | Пропустить перевод (только конвертация в Markdown) |
| `--github-token` | Personal Access Token для GitHub API |
| `--workers N` | Количество потоков (по умолчанию: 4) |

## Лицензия

Весь контент SCP распространяется по лицензии
[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) — см. [LICENSE](./LICENSE).

Перевод является производным произведением и распространяется на тех же условиях.
"""
    Path("README.md").write_text(content, encoding="utf-8")
    log.info("README.md written.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--section", choices=list(SECTIONS), default=None,
                   help="Process only this section (default: all)")
    p.add_argument("--limit", type=int, default=None,
                   help="Max articles per section")
    p.add_argument("--no-translate", action="store_true",
                   help="Skip argostranslate translation step")
    p.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN"),
                   help="GitHub Personal Access Token (or set GITHUB_TOKEN env var)")
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel translation/write workers (default: 4)")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    do_translate = not args.no_translate

    client = GithubClient(token=args.github_token)

    # Pre-init translator (downloads model once)
    if do_translate:
        log.info("Initialising argostranslate en→ru …")
        if _init_translator() is None:
            log.error("Translation unavailable. Re-run with --no-translate to skip.")
            sys.exit(1)

    sections_to_run = {args.section: SECTIONS[args.section]} if args.section else SECTIONS
    results: dict[str, int] = {}

    for section, cfg in sections_to_run.items():
        try:
            count = process_section(
                section, cfg, client,
                do_translate=do_translate,
                limit=args.limit,
                workers=args.workers,
            )
            results[section] = count
        except Exception as exc:
            log.error("[%s] Section failed: %s", section, exc)
            results[section] = 0

    write_readme(results)

    total = sum(results.values())
    log.info("All done. Total articles written: %d", total)
    log.info("Output: %s/", OUTPUT_DIR)


if __name__ == "__main__":
    main()
