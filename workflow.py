#!/usr/bin/env python3
"""
SCP API → Markdown + Russian translation workflow.

Two data-source modes:
  --local-path PATH   Read from a pre-cloned scp-api repo (docs/data/scp/)
  (no flag)           Download files via GitHub raw URLs (cached in ./cache/)

Usage:
    python workflow.py [--section SECTION] [--limit N] [--no-translate]
                       [--local-path PATH] [--github-token TOKEN] [--workers N]

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

SECTIONS = {
    "items": {
        "label": "Объект СЦП",
        "remote": {
            "index": f"{RAW_BASE}/items/index.json",
            "content_index": f"{RAW_BASE}/items/content_index.json",
            "content_base_url": f"{RAW_BASE}/items/",
        },
        "local": {
            "index": Path("items/index.json"),
            "content_index": Path("items/content_index.json"),
            "content_dir": Path("items/"),
        },
    },
    "tales": {
        "label": "Рассказ",
        "remote": {"index": f"{RAW_BASE}/tales/index.json"},
        "local": {"index": Path("tales/index.json")},
    },
    "hubs": {
        "label": "Хаб",
        "remote": {"index": f"{RAW_BASE}/hubs/index.json"},
        "local": {"index": Path("hubs/index.json")},
    },
    "goi": {
        "label": "Группа Интересов",
        "remote": {"index": f"{RAW_BASE}/goi/index.json"},
        "local": {"index": Path("goi/index.json")},
    },
}

OUTPUT_DIR = Path("output")
CACHE_DIR = Path("cache")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

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

    def download_raw(self, url: str, dest: Path) -> Path:
        """Download a potentially large raw file with streaming, cache on disk."""
        if dest.exists():
            log.debug("Cache hit: %s", dest.name)
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        log.info("Downloading %s …", url.split("/")[-1])
        for attempt in range(5):
            try:
                with self.session.get(url, stream=True, timeout=120) as resp:
                    if resp.status_code in (429, 403):
                        reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                        wait = max(reset - int(time.time()), 5)
                        log.warning("Rate limited — sleeping %ds", wait)
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    with open(tmp, "wb") as fh, tqdm(
                        total=total, unit="B", unit_scale=True,
                        desc=dest.name, leave=False,
                    ) as bar:
                        for chunk in resp.iter_content(chunk_size=1 << 16):
                            fh.write(chunk)
                            bar.update(len(chunk))
                tmp.rename(dest)
                return dest
            except requests.RequestException as exc:
                log.warning("Download error (attempt %d): %s", attempt + 1, exc)
                time.sleep(5)
        raise RuntimeError(f"Failed to download {url} after retries")


# ---------------------------------------------------------------------------
# File resolution (local vs remote)
# ---------------------------------------------------------------------------

def resolve_json(
    local_path: Optional[Path],
    remote_url: str,
    cache_path: Path,
    client: GithubClient,
) -> Path:
    """Return a local filesystem path to the JSON file."""
    if local_path is not None:
        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")
        return local_path
    return client.download_raw(remote_url, cache_path)


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


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
    """Translate a text block en→ru paragraph by paragraph."""
    if not text or not text.strip():
        return text
    tr = _init_translator()
    if tr is None:
        return text
    result = []
    for para in text.split("\n"):
        if para.strip():
            try:
                result.append(tr.translate(para))
            except Exception as exc:
                log.debug("Translation error: %s", exc)
                result.append(para)
        else:
            result.append(para)
    return "\n".join(result)


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
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p>|</div>|</li>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", "", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def item_to_markdown(link: str, data: dict, section_label: str, do_translate: bool) -> str:
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
    lines.append(f"# {title_ru}")
    lines.append("")
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
    if tags:
        lines.append("**Теги:** " + " ".join(f"`{t}`" for t in tags))
        lines.append("")
    if images:
        lines.append("## Изображения")
        lines.append("")
        for img in images[:5]:
            lines.append(f"![]({img})")
        lines.append("")
    if content_ru:
        lines.append("## Содержание")
        lines.append("")
        lines.append(content_ru)
        lines.append("")
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
    (out_dir / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Section processing
# ---------------------------------------------------------------------------

def _process_entry(args: tuple) -> tuple[str, bool]:
    link, data, label, do_translate, out_dir = args
    try:
        md = item_to_markdown(link, data, label, do_translate)
        write_markdown(out_dir, link, md)
        return link, True
    except Exception as exc:
        log.warning("Failed %s: %s", link, exc)
        return link, False


def process_section(
    section: str,
    cfg: dict,
    client: GithubClient,
    do_translate: bool,
    limit: Optional[int],
    workers: int,
    local_base: Optional[Path] = None,
) -> int:
    out_dir = OUTPUT_DIR / section
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = CACHE_DIR / section
    cache_dir.mkdir(parents=True, exist_ok=True)

    label = cfg["label"]
    local_cfg = cfg["local"]
    remote_cfg = cfg["remote"]

    index_path = resolve_json(
        local_path=local_base / local_cfg["index"] if local_base else None,
        remote_url=remote_cfg["index"],
        cache_path=cache_dir / "index.json",
        client=client,
    )
    log.info("[%s] Loading index …", section)
    index: dict = load_json(index_path)

    success = 0

    if section == "items":
        # Process per-series to avoid loading all content into memory at once
        content_index_path = resolve_json(
            local_path=local_base / local_cfg["content_index"] if local_base else None,
            remote_url=remote_cfg["content_index"],
            cache_path=cache_dir / "content_index.json",
            client=client,
        )
        content_index: dict = load_json(content_index_path)

        written = 0
        for series_name, filename in content_index.items():
            if limit and written >= limit:
                break

            local_file = (local_base / local_cfg["content_dir"] / filename) if local_base else None
            content_path = resolve_json(
                local_path=local_file,
                remote_url=remote_cfg["content_base_url"] + filename,
                cache_path=cache_dir / filename,
                client=client,
            )
            log.info("[items] Loading series: %s …", series_name)
            series_data: dict = load_json(content_path)

            # Merge index metadata into content entries
            entries = []
            for link, content_entry in series_data.items():
                if limit and written + len(entries) >= limit:
                    break
                meta = index.get(link, {})
                merged = {**meta, **content_entry}
                entries.append((link, merged, label, do_translate, out_dir))

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_process_entry, e): e[0] for e in entries}
                with tqdm(total=len(entries), desc=f"items/{series_name}", unit="art") as bar:
                    for fut in as_completed(futures):
                        _, ok = fut.result()
                        if ok:
                            success += 1
                        written += 1
                        bar.update(1)

            del series_data  # free memory before next series

    else:
        entries_raw = list(index.items())
        if limit:
            entries_raw = entries_raw[:limit]
        entries = [(lnk, d, label, do_translate, out_dir) for lnk, d in entries_raw]

        log.info("[%s] Processing %d entries …", section, len(entries))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_process_entry, e): e[0] for e in entries}
            with tqdm(total=len(entries), desc=section, unit="art") as bar:
                for fut in as_completed(futures):
                    _, ok = fut.result()
                    if ok:
                        success += 1
                    bar.update(1)

    log.info("[%s] Done — %d written", section, success)
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
pip install -r requirements.txt

# Из локально склонированного репо
python workflow.py --local-path scp-api/docs/data/scp

# Через GitHub API (кэширует в cache/)
python workflow.py --section items --limit 100 --no-translate
```

## Лицензия

Весь контент SCP Wiki распространяется по лицензии
[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) — см. [LICENSE](./LICENSE).
"""
    Path("README.md").write_text(content, encoding="utf-8")
    log.info("README.md written.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--section", choices=list(SECTIONS), default=None,
                   help="Process only this section (default: all)")
    p.add_argument("--limit", type=int, default=None,
                   help="Max articles per section")
    p.add_argument("--no-translate", action="store_true",
                   help="Skip argostranslate — only convert JSON→Markdown")
    p.add_argument("--local-path", type=Path, default=None, metavar="PATH",
                   help="Path to docs/data/scp/ inside a cloned scp-api repo")
    p.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN"),
                   help="GitHub PAT for remote mode (or set GITHUB_TOKEN env var)")
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel write workers (default: 4)")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    local_base: Optional[Path] = args.local_path
    if local_base is not None:
        if not local_base.is_dir():
            log.error("--local-path does not exist: %s", local_base)
            sys.exit(1)
        log.info("Local mode: reading from %s", local_base)
    else:
        log.info("Remote mode: downloading via GitHub raw URLs")

    do_translate = not args.no_translate
    if do_translate:
        log.info("Initialising argostranslate en→ru …")
        if _init_translator() is None:
            log.error("Translation unavailable. Re-run with --no-translate to skip.")
            sys.exit(1)

    client = GithubClient(token=args.github_token)
    sections_to_run = {args.section: SECTIONS[args.section]} if args.section else SECTIONS
    results: dict[str, int] = {}

    for section, cfg in sections_to_run.items():
        try:
            count = process_section(
                section=section,
                cfg=cfg,
                client=client,
                do_translate=do_translate,
                limit=args.limit,
                workers=args.workers,
                local_base=local_base,
            )
            results[section] = count
        except Exception as exc:
            log.error("[%s] Section failed: %s", section, exc)
            results[section] = 0

    write_readme(results)
    log.info("Done. Total: %d articles. Output: %s/", sum(results.values()), OUTPUT_DIR)


if __name__ == "__main__":
    main()
