from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import fitz

from app.schemas.note_schema import Note


_PAGE = fitz.paper_rect("a4")
_MARGIN_X = 58
_MARGIN_TOP = 62
_MARGIN_BOTTOM = 58
_LINE = 1.42
_MATH_DPI = 180


def safe_pdf_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", (title or "note").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return f"{name[:80] or 'note'}.pdf"


def export_note_to_pdf(note: Note, output_path: str | os.PathLike[str]) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    font = _pick_font()
    doc = fitz.open()
    page = doc.new_page(width=_PAGE.width, height=_PAGE.height)
    cursor = _Cursor(doc=doc, page=page, font=font)

    cursor.text(note.title or "Untitled Note", size=22, color=(0.07, 0.09, 0.14), gap_after=14)
    meta = _format_meta(note)
    if meta:
        cursor.text(meta, size=9.5, color=(0.39, 0.45, 0.55), gap_after=16)
    cursor.rule()

    for block in _markdown_blocks(note.content_markdown or ""):
        kind, text = block
        if kind == "blank":
            cursor.space(8)
        elif kind == "h1":
            cursor.text(text, size=17, color=(0.07, 0.09, 0.14), gap_before=10, gap_after=7)
        elif kind == "h2":
            cursor.text(text, size=15, color=(0.07, 0.09, 0.14), gap_before=9, gap_after=6)
        elif kind == "h3":
            cursor.text(text, size=13.5, color=(0.13, 0.16, 0.22), gap_before=7, gap_after=5)
        elif kind == "quote":
            cursor.text(text, size=11, color=(0.29, 0.34, 0.42), indent=14, gap_after=5)
        elif kind == "code":
            cursor.text(text, size=10.5, color=(0.13, 0.16, 0.22), indent=12, gap_before=4, gap_after=7)
        elif kind == "math":
            cursor.math(text, display=True)
        elif kind == "bullet":
            cursor.rich_text(f"- {text}", size=11.5, color=(0.13, 0.16, 0.22), indent=10, gap_after=4)
        else:
            cursor.rich_text(text, size=11.5, color=(0.13, 0.16, 0.22), gap_after=6)

    doc.save(output)
    doc.close()
    return output


class _Cursor:
    def __init__(self, doc: fitz.Document, page: fitz.Page, font: str | None):
        self.doc = doc
        self.page = page
        self.font = font
        self.font_obj = fitz.Font(fontfile=font) if font else None
        self.y = _MARGIN_TOP
        self.width = _PAGE.width - 2 * _MARGIN_X

    def space(self, value: float) -> None:
        self.y += value

    def rule(self) -> None:
        self._ensure(18)
        y = self.y
        self.page.draw_line((_MARGIN_X, y), (_PAGE.width - _MARGIN_X, y), color=(0.90, 0.92, 0.95), width=0.8)
        self.y += 18

    def text(
        self,
        text: str,
        *,
        size: float,
        color: tuple[float, float, float],
        indent: float = 0,
        gap_before: float = 0,
        gap_after: float = 0,
    ) -> None:
        if not text:
            return
        self.y += gap_before
        x = _MARGIN_X + indent
        max_width = self.width - indent
        line_height = size * _LINE
        for raw in str(text).splitlines() or [""]:
            for line in _wrap_line(raw, max_width, size, self.font_obj):
                self._ensure(line_height)
                kwargs = {"fontsize": size, "color": color}
                if self.font:
                    kwargs.update({"fontfile": self.font, "fontname": "note-font"})
                self.page.insert_text((x, self.y), line, **kwargs)
                self.y += line_height
        self.y += gap_after

    def rich_text(
        self,
        text: str,
        *,
        size: float,
        color: tuple[float, float, float],
        indent: float = 0,
        gap_before: float = 0,
        gap_after: float = 0,
    ) -> None:
        parts = _split_inline_math(text)
        if len(parts) == 1 and parts[0][0] == "text":
            self.text(text, size=size, color=color, indent=indent, gap_before=gap_before, gap_after=gap_after)
            return
        self.y += gap_before
        self._inline_parts(parts, size=size, color=color, indent=indent)
        self.y += gap_after

    def _inline_parts(
        self,
        parts: list[tuple[str, str]],
        *,
        size: float,
        color: tuple[float, float, float],
        indent: float = 0,
    ) -> None:
        x0 = _MARGIN_X + indent
        max_x = _MARGIN_X + self.width
        x = x0
        line_height = size * _LINE
        self._ensure(line_height)
        baseline = self.y
        text_kwargs = {"fontsize": size, "color": color}
        if self.font:
            text_kwargs.update({"fontfile": self.font, "fontname": "note-font"})

        def new_line(extra: float = 0) -> None:
            nonlocal x, baseline
            self.y = baseline + max(line_height, extra)
            self._ensure(line_height)
            x = x0
            baseline = self.y

        for kind, value in parts:
            if kind == "math":
                rendered = _render_math_png(value, display=False)
                if rendered:
                    png_path, width, height = rendered
                    try:
                        scale = min(1.0, (max_x - x0) / max(width, 1))
                        w = width * scale
                        h = height * scale
                        if x > x0 and x + w > max_x:
                            new_line(h + 3)
                        rect = fitz.Rect(x, baseline - h + 3, x + w, baseline + 3)
                        self.page.insert_image(rect, filename=str(png_path))
                        x += w + 2
                    finally:
                        _unlink_later(png_path)
                else:
                    value = _math_fallback(value, inline=True)
                    for segment in _text_segments(value):
                        seg_width = _text_width(segment, size, self.font_obj)
                        if x > x0 and x + seg_width > max_x:
                            new_line()
                        self.page.insert_text((x, baseline), segment, **text_kwargs)
                        x += seg_width
                continue

            for segment in _text_segments(value):
                if "\n" in segment:
                    new_line()
                    continue
                seg_width = _text_width(segment, size, self.font_obj)
                if x > x0 and x + seg_width > max_x:
                    new_line()
                self.page.insert_text((x, baseline), segment, **text_kwargs)
                x += seg_width
        self.y = baseline + line_height

    def math(self, latex: str, *, display: bool = False) -> None:
        rendered = _render_math_png(latex, display=display)
        if not rendered:
            self.text(_math_fallback(latex, inline=not display), size=11.5, color=(0.13, 0.16, 0.22), indent=14, gap_after=8)
            return
        png_path, width, height = rendered
        try:
            max_width = min(self.width - 28, width)
            scale = max_width / width if width > max_width else 1
            w = width * scale
            h = height * scale
            self._ensure(h + 16)
            x = _MARGIN_X + (self.width - w) / 2 if display else _MARGIN_X
            rect = fitz.Rect(x, self.y + 4, x + w, self.y + 4 + h)
            self.page.insert_image(rect, filename=str(png_path))
            self.y += h + 16
        finally:
            _unlink_later(png_path)

    def _ensure(self, needed: float) -> None:
        if self.y + needed <= _PAGE.height - _MARGIN_BOTTOM:
            return
        self.page = self.doc.new_page(width=_PAGE.width, height=_PAGE.height)
        self.y = _MARGIN_TOP


def _wrap_line(text: str, max_width: float, size: float, font: fitz.Font | None) -> list[str]:
    text = text.rstrip()
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for ch in text:
        trial = current + ch
        if current and _text_width(trial, size, font) > max_width:
            lines.append(current.rstrip())
            current = ch.lstrip()
        else:
            current = trial
    if current:
        lines.append(current.rstrip())
    return lines or [""]


def _text_segments(text: str) -> list[str]:
    segments: list[str] = []
    for piece in re.split(r"(\s+)", str(text)):
        if not piece:
            continue
        if piece.isspace():
            segments.append(piece)
            continue
        if len(piece) > 32:
            segments.extend(list(piece))
        else:
            segments.append(piece)
    return segments


def _text_width(text: str, size: float, font: fitz.Font | None) -> float:
    if font:
        return font.text_length(text, fontsize=size)
    return fitz.get_text_length(text, fontsize=size, fontname="helv")


def _markdown_blocks(markdown: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    in_code = False
    in_math = False
    code_lines: list[str] = []
    math_lines: list[str] = []
    for line in markdown.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("\\["):
            remainder = stripped[2:]
            if in_math:
                end = remainder[:-2] if remainder.endswith("\\]") else remainder
                if end.strip():
                    math_lines.append(end)
                blocks.append(("math", "\n".join(math_lines).strip()))
                math_lines = []
                in_math = False
            elif stripped.endswith("\\]") and len(stripped) > 4:
                blocks.append(("math", stripped[2:-2].strip()))
            else:
                in_math = True
                if remainder.strip():
                    math_lines.append(remainder)
            continue
        if in_math and stripped.endswith("\\]"):
            end = stripped[:-2]
            if end.strip():
                math_lines.append(end)
            blocks.append(("math", "\n".join(math_lines).strip()))
            math_lines = []
            in_math = False
            continue
        if stripped.startswith("$$"):
            remainder = stripped[2:]
            if in_math:
                end = remainder[:-2] if remainder.endswith("$$") else remainder
                if end.strip():
                    math_lines.append(end)
                blocks.append(("math", "\n".join(math_lines).strip()))
                math_lines = []
                in_math = False
            elif stripped.endswith("$$") and len(stripped) > 4:
                blocks.append(("math", stripped[2:-2].strip()))
            else:
                in_math = True
                if remainder.strip():
                    math_lines.append(remainder)
            continue
        if in_math:
            math_lines.append(line)
            continue
        if stripped.startswith("```"):
            if in_code:
                blocks.append(("code", "\n".join(code_lines)))
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            blocks.append(("blank", ""))
            continue
        if m := re.match(r"^(#{1,3})\s+(.+)$", stripped):
            blocks.append((f"h{len(m.group(1))}", _clean_inline(m.group(2))))
        elif m := re.match(r"^[-*+]\s+(.+)$", stripped):
            blocks.append(("bullet", _clean_inline(m.group(1))))
        elif m := re.match(r"^\d+[.)]\s+(.+)$", stripped):
            blocks.append(("bullet", _clean_inline(m.group(1))))
        elif stripped.startswith(">"):
            blocks.append(("quote", _clean_inline(stripped.lstrip("> ").strip())))
        else:
            blocks.append(("p", _clean_inline(stripped)))
    if code_lines:
        blocks.append(("code", "\n".join(code_lines)))
    if math_lines:
        blocks.append(("math", "\n".join(math_lines).strip()))
    return blocks


def _clean_inline(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = text.replace("~~", "")
    return text


def _split_inline_math(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    pattern = r"(?<!\\)\$(.+?)(?<!\\)\$|\\\((.+?)\\\)"
    pos = 0
    for m in re.finditer(pattern, text):
        if m.start() > pos:
            parts.append(("text", text[pos:m.start()]))
        parts.append(("math", (m.group(1) or m.group(2) or "").strip()))
        pos = m.end()
    if pos < len(text):
        parts.append(("text", text[pos:]))
    return parts or [("text", text)]


def _render_math_png(latex: str, *, display: bool) -> tuple[Path, float, float] | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None

    expr = _normalize_math_expr(latex)
    if not expr:
        return None
    if re.search(r"[\u4e00-\u9fff]", expr):
        return None
    expr = expr.replace("\n", " ")
    if not (expr.startswith("$") and expr.endswith("$")):
        expr = f"${expr}$"
    try:
        fig = plt.figure(figsize=(0.01, 0.01), dpi=_MATH_DPI)
        fig.patch.set_alpha(0)
        text = fig.text(0, 0, expr, fontsize=14 if display else 11, color="#1f2937")
        fig.canvas.draw()
        bbox = text.get_window_extent()
        width = max(1, bbox.width / _MATH_DPI)
        height = max(1, bbox.height / _MATH_DPI)
        fig.set_size_inches(width, height)
        path = Path(tempfile.mkstemp(suffix=".png")[1])
        fig.savefig(path, dpi=_MATH_DPI, transparent=True, bbox_inches="tight", pad_inches=0.04)
        plt.close(fig)
        pix = fitz.Pixmap(str(path))
        pdf_width = pix.width * 72 / _MATH_DPI
        pdf_height = pix.height * 72 / _MATH_DPI
        pix = None
        return path, pdf_width, pdf_height
    except Exception:
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def _normalize_math_expr(latex: str) -> str:
    expr = latex.strip().strip("$").strip()
    expr = re.sub(r"\\begin\{(?:equation\*?|align\*?|aligned|gather\*?|gathered|split)\}", "", expr)
    expr = re.sub(r"\\end\{(?:equation\*?|align\*?|aligned|gather\*?|gathered|split)\}", "", expr)
    expr = re.sub(r"\\text\{([^{}]*)\}", r"\\mathrm{\1}", expr)
    expr = expr.replace("&", "")
    expr = re.sub(r"\\\\\s*", r"  ", expr)
    expr = re.sub(r"\s+", " ", expr).strip()
    return expr


def _math_fallback(latex: str, *, inline: bool) -> str:
    text = _normalize_math_expr(latex)
    replacements = {
        r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
        r"\epsilon": "ε", r"\varepsilon": "ε", r"\theta": "θ", r"\lambda": "λ",
        r"\mu": "μ", r"\pi": "π", r"\sigma": "σ", r"\phi": "φ",
        r"\varphi": "φ", r"\omega": "ω", r"\Omega": "Ω", r"\Phi": "Φ",
        r"\times": "×", r"\cdot": "·", r"\leq": "≤", r"\geq": "≥",
        r"\le": "≤", r"\ge": "≥", r"\neq": "≠", r"\approx": "≈",
        r"\infty": "∞", r"\sum": "Σ",
        r"\prod": "Π", r"\int": "∫", r"\partial": "∂", r"\nabla": "∇",
        r"\rightarrow": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒", r"\to": "→",
        r"\in": "∈", r"\notin": "∉", r"\subset": "⊂", r"\subseteq": "⊆",
        r"\cup": "∪", r"\cap": "∩", r"\forall": "∀", r"\exists": "∃",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\\sqrt\{([^{}]+)\}", r"√(\1)", text)
    text = re.sub(r"\\(?:left|right)[()\[\]{}|.]?", "", text)
    text = text.replace(r"\|", "|")
    text = re.sub(r"\\[a-zA-Z]+", "", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("^2", "²").replace("^3", "³")
    text = re.sub(r"_\{([^{}]+)\}", r"₍\1₎", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if inline else f"公式：{text}"


def _unlink_later(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _format_meta(note: Note) -> str:
    parts = []
    if note.updated_at:
        parts.append(f"Updated: {_fmt_time(note.updated_at)}")
    if note.tags:
        parts.append("Tags: " + ", ".join(note.tags))
    if note.embedding_status:
        parts.append(f"Embedding: {note.embedding_status}")
    return "   |   ".join(parts)


def _fmt_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _pick_font() -> str | None:
    candidates = [
        os.getenv("NOTE_PDF_FONT"),
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None
