"""Stage only released student material, then render it with Quarto.

Private content is never a Quarto project root or a Pages artifact.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MONTHS = ("", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
          "août", "septembre", "octobre", "novembre", "décembre")
SUFFIXES = {".ipynb", ".py", ".sh", ".bash", ".r", ".jl", ".sql", ".csv", ".tsv",
            ".json", ".yaml", ".yml", ".toml", ".txt", ".wav", ".mp3", ".flac",
            ".png", ".jpg", ".jpeg", ".svg", ".webp", ".parquet", ".onnx"}


def timestamp(value):
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("starts_at doit inclure un fuseau explicite, par exemple +01:00")
    return parsed


def catalog(path):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("courses"), list):
        raise ValueError("catalog.yml doit contenir une liste courses")
    seen = set()
    for course in data["courses"]:
        slug(course["id"])
        if course["id"] in seen:
            raise ValueError("Identifiant de cours dupliqué")
        seen.add(course["id"])
        ZoneInfo(course["timezone"])
        session_ids = set()
        for session in course["sessions"]:
            slug(session["id"])
            if session["id"] in session_ids:
                raise ValueError("Identifiant de séance dupliqué")
            session_ids.add(session["id"])
            timestamp(session.get("starts_at"))
            if not isinstance(session.get("ready"), bool):
                raise ValueError("ready doit être un booléen")
    return data


def slug(value):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ValueError("Identifiant invalide")
    return value


def released(session, now):
    start = timestamp(session.get("starts_at"))
    return session["ready"] and start is not None and now >= start


def date_label(session, course):
    start = timestamp(session.get("starts_at"))
    if start is None:
        return "Date à confirmer"
    dt = start.astimezone(ZoneInfo(course["timezone"]))
    offset = dt.strftime("%z")
    return (f"{dt.day} {MONTHS[dt.month]} {dt.year} à {dt:%H}h{dt:%M} "
            f"({course['timezone']}, UTC{offset[:3]}:{offset[3:]})")


def private_file(root, relative):
    """Confine each explicitly selected path; reject symlink components."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Le répertoire étudiant ne peut pas être un lien symbolique")
    root = root.resolve()
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(p in {"..", "."} or p.startswith(".") for p in rel.parts):
        raise ValueError("Chemin de ressource invalide")
    candidate = root
    for part in rel.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Les liens symboliques ne sont pas publiables")
    if not candidate.resolve().is_relative_to(root) or not candidate.is_file():
        raise ValueError("Ressource étudiante absente ou hors du répertoire autorisé")
    return candidate


def clean_notebook(source, destination):
    nb = json.loads(source.read_text(encoding="utf-8"))
    if nb.get("nbformat") != 4 or not isinstance(nb.get("cells"), list):
        raise ValueError("Notebook Jupyter v4 attendu")
    # Keep source code and Markdown; discard outputs, widgets and private metadata.
    nb["metadata"] = {k: v for k, v in nb.get("metadata", {}).items()
                      if k in {"kernelspec", "language_info"}}
    for cell in nb["cells"]:
        cell["metadata"] = {}
        if cell["cell_type"] == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
    destination.write_text(json.dumps(nb, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def document(title, body):
    header = {"title": title, "engine": "markdown", "execute": {"enabled": False}}
    return "---\n" + yaml.safe_dump(header, allow_unicode=True, sort_keys=False) + "---\n\n" + body + "\n"


def article_body(source):
    text = source.read_text(encoding="utf-8")
    # Page metadata is controlled by the engine; author the article in Markdown/LaTeX.
    if text.startswith("---\n"):
        parts = text.split("\n---", 1)
        if len(parts) != 2:
            raise ValueError("En-tête YAML de l'article non terminé")
        text = parts[1].lstrip("\r\n")
    return text


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def stage(catalog_path, content_root, output, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("L'horloge doit avoir un fuseau explicite")
    courses = catalog(catalog_path)["courses"]
    output = Path(output)
    # Never reuse a previous staging tree: deleted or locked material must not linger.
    if output.exists():
        raise ValueError("Le répertoire de staging doit être nouveau")
    output.mkdir(parents=True)
    student_root = Path(content_root) / "student"
    html_pages = ["index.qmd"]
    pdf_pages = []
    sidebars = []
    cards = []
    for course in courses:
        cid = course["id"]
        cdir = output / "cours" / cid
        nav = [{"text": "Vue d'ensemble", "href": f"cours/{cid}/index.qmd"}]
        session_cards = []
        opened = 0
        for index, session in enumerate(course["sessions"], 1):
            sid = session["id"]
            is_open = released(session, now)
            title = html.escape(session["title"])
            date = html.escape(date_label(session, course))
            button = "Les ressources seront disponibles au début de la séance."
            status = "À venir"
            if is_open:
                opened += 1
                status = "Disponible"
                # Only due sessions are even read from the private repository.
                sroot = student_root
                body = article_body(private_file(sroot, f"{cid}/{sid}/article.qmd"))
                manifest = yaml.safe_load(private_file(sroot, f"{cid}/{sid}/resources.yml").read_text(encoding="utf-8"))
                if not isinstance(manifest, dict) or not isinstance(manifest.get("resources"), list):
                    raise ValueError("resources.yml doit contenir une liste resources")
                links = []
                resource_paths = set()
                for resource in manifest["resources"]:
                    rel = resource["path"]
                    if rel in resource_paths:
                        raise ValueError("Ressource dupliquée")
                    resource_paths.add(rel)
                    # Keep URLs stable and unambiguous, including on Windows.
                    if not re.fullmatch(r"[a-zA-Z0-9_./-]+", rel):
                        raise ValueError("Utiliser des noms de ressources sans espaces ni caractères spéciaux")
                    src = private_file(sroot, f"{cid}/{sid}/resources/{rel}")
                    if src.suffix.lower() not in SUFFIXES:
                        raise ValueError("Type de ressource non autorisé (les PDF sont générés)")
                    dst = cdir / sid / "resources" / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if src.suffix.lower() == ".ipynb":
                        clean_notebook(src, dst)
                    else:
                        shutil.copyfile(src, dst)
                    label = html.escape(resource.get("label", rel))
                    links.append(f'<li><a href="resources/{rel}" download>{label}</a></li>')
                downloads = ('\n\n::: {.content-visible when-format="html"}\n\n## Ressources\n\n'
                             '<p><a class="btn btn-outline-primary" href="index.pdf" download>Télécharger le cours en PDF</a></p>\n'
                             '<ul class="resource-list">' + "".join(links) + '</ul>\n\n:::\n')
                page = f"cours/{cid}/{sid}/index.qmd"
                write(output / page, document(session["title"], body + downloads))
                html_pages.append(page)
                pdf_pages.append({"source": page, "course": cid, "title": session["title"]})
                nav.append({"text": session["title"], "href": page})
                button = f'<a class="btn btn-primary" href="{sid}/index.qmd">Accéder à la séance</a>'
            else:
                nav.append({"text": f"{index}. {session['title']} · À venir"})
            session_cards.append(
                f'<section class="session-card"><span class="eyebrow">Séance {index:02}</span>'
                f'<h2>{title}</h2><p class="session-date">{date}</p>'
                f'<p><span class="status status-{"open" if is_open else "locked"}">{status}</span></p>'
                f'<p>{button}</p></section>')
        count = len(course["sessions"])
        course_body = (f'<p>{html.escape(course["description"])}</p>\n'
                       f'<p class="course-progress">{opened} séance(s) disponible(s) sur {count}.</p>\n')
        if opened:
            course_body += '<p><a class="btn btn-outline-primary" href="cours-disponible.pdf" download>Télécharger toutes les séances disponibles en PDF</a></p>\n'
        write(cdir / "index.qmd", document(course["title"], course_body + "\n".join(session_cards)))
        html_pages.append(f"cours/{cid}/index.qmd")
        sidebars.append({"id": cid, "title": course["title"], "style": "docked", "contents": nav})
        cards.append(f'<section class="course-card"><span class="eyebrow">{count} séances · {opened} disponible(s)</span>'
                     f'<h2><a href="cours/{cid}/index.qmd">{html.escape(course["title"])}</a></h2>'
                     f'<p>{html.escape(course["description"])}</p>'
                     f'<a class="btn btn-primary" href="cours/{cid}/index.qmd">Découvrir le cours</a></section>')
    write(output / "index.qmd", document("Les cours Brainbrew",
          '<div class="catalog-intro">Des cours pour comprendre, expérimenter et construire. '
          'Retrouvez votre programme, les prochaines séances et les ressources disponibles.</div>\n' + "\n".join(cards)))
    shutil.copyfile(ROOT / "assets/theme.scss", output / "theme.scss")
    config = {
        "project": {"type": "website", "output-dir": "_site", "render": html_pages,
                    "resources": ["cours/**/resources/**", "brainbrew-release.json"]},
        "lang": "fr",
        "format-links": False,
        "website": {"title": "brainbrew", "site-url": "https://hirecoffee.github.io/brainbrew/",
                    "navbar": {"left": [{"text": "Catalogue", "href": "index.qmd"}]},
                    "sidebar": sidebars, "search": True, "page-navigation": True,
                    "page-footer": "Brainbrew · Apprendre en pratiquant"},
        "format": {"html": {"theme": ["cosmo", "theme.scss"], "toc": True,
                            "html-math-method": {"method": "mathjax", "url": "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-svg-full.js"},
                            "code-copy": True},
                   "pdf": {"pdf-engine": "xelatex", "toc": True, "number-sections": True,
                           "colorlinks": True, "papersize": "a4", "callout-icon": False}},
        "execute": {"enabled": False},
    }
    write(output / "_quarto.yml", yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    fingerprint = hashlib.sha256()
    for path in sorted(p for p in output.rglob("*") if p.is_file()):
        fingerprint.update(str(path.relative_to(output)).encode())
        fingerprint.update(b"\0")
        fingerprint.update(path.read_bytes())
        fingerprint.update(b"\0")
    fingerprint.update(Path(__file__).read_bytes())
    fingerprint.update((ROOT / "requirements.txt").read_bytes())
    write(output / "brainbrew-release.json", json.dumps({"fingerprint": fingerprint.hexdigest()}) + "\n")
    # Build instructions stay outside the directory uploaded to GitHub Pages.
    write(output.with_name(output.name + ".plan.json"), json.dumps(pdf_pages))
    return pdf_pages


def render(output, pdf_pages, quarto="quarto"):
    output = Path(output).resolve()
    subprocess.run([quarto, "render", str(output), "--to", "html", "--no-execute"], check=True)
    by_course = {}
    for page in pdf_pages:
        source = output / page["source"]
        subprocess.run([quarto, "render", str(source), "--to", "pdf", "--no-execute"], check=True)
        pdf = output / "_site" / Path(page["source"]).with_suffix(".pdf")
        if not pdf.is_file():
            raise RuntimeError("Le rendu PDF attendu est absent")
        by_course.setdefault(page["course"], []).append((pdf, page["title"]))
    if by_course:
        from pypdf import PdfWriter
        for cid, pages in by_course.items():
            writer = PdfWriter()
            for pdf, title in pages:
                writer.append(str(pdf), outline_item=title)
            with (output / "_site" / "cours" / cid / "cours-disponible.pdf").open("wb") as stream:
                writer.write(stream)
            writer.close()
    (output / "_site" / ".nojekyll").touch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "catalog.yml")
    parser.add_argument("--content-root", type=Path, default=ROOT / "private-content")
    parser.add_argument("--output", type=Path, default=ROOT / "build/site")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--render-staged", action="store_true")
    parser.add_argument("--compare-published", action="store_true")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--quarto", default="quarto")
    args = parser.parse_args()
    if args.render_staged:
        jobs = json.loads(args.output.with_name(args.output.name + ".plan.json").read_text())
        render(args.output, jobs, args.quarto)
        return
    jobs = stage(args.catalog, args.content_root, args.output)
    changed = True
    if args.compare_published:
        # Only a public fingerprint is fetched. A missing site triggers an initial build.
        try:
            with urlopen("https://hirecoffee.github.io/brainbrew/brainbrew-release.json", timeout=15) as response:
                published = json.load(response)
            current = json.loads((args.output / "brainbrew-release.json").read_text())
            changed = published != current
        except (HTTPError, URLError, TimeoutError, ValueError):
            pass
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"changed={str(changed).lower()}\n")
    if args.render and changed:
        render(args.output, jobs, args.quarto)
    print(f"Site préparé : {len(jobs)} séance(s) publiée(s).")


if __name__ == "__main__":
    main()
