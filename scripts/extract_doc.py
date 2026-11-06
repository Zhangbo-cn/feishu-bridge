#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把办公文档抽成纯文本 —— 给 agent 读的, 不是给人看排版的。

    python3 extract_doc.py <文件> [--max-chars N] [--json]

为什么不用 python-docx / markitdown
-----------------------------------
docx / xlsx / pptx 都是 **zip + XML**, 标准库 `zipfile` + `xml.etree` 就能解。
这个 skill 的发送侧是零依赖的, 解析侧也尽量保持 —— 否则"在任意机器上 clone 就能用"
这个前提就没了 (系统 python3 常常只有标准库)。

只有 PDF 没法用标准库 (二进制流 + 压缩 + 字体编码), 需要 pymupdf / pdftotext,
两个都没有时给出可执行的安装提示, 而不是抛一个看不懂的 ImportError。

支持
----
  .docx .doc(仅docx伪装)   段落 + 表格 + 标题层级
  .xlsx .xlsm              每个 sheet 转 TSV (公式取缓存值)
  .pptx                    每页文字 + 备注
  .pdf                     pymupdf → pdftotext → 报错
  .txt .md .csv .json .py 等文本类   直接读 (猜编码)
  其他                     报错并说明

输出口径
--------
文本结构用 `## 小节` / `| 表格 |` 这类轻标记, 因为下游是 LLM:
保留结构能让它答"第几节说了什么", 纯 flatten 就丢了。
截断时**在末尾标注原长度**, 不静默截断 —— 否则 agent 会以为自己读完了。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

MAX_DEFAULT = 200_000       # 约 50k token, 够大多数文档; 超了截断并标注

# OOXML 命名空间。docx/xlsx/pptx 各一套, 写全免得 findall 时拼字符串。
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
TEXTY = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
         ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".go", ".rs", ".sh", ".sql",
         ".html", ".htm", ".xml", ".ini", ".conf", ".toml", ".log", ".rst"}


class ExtractError(RuntimeError):
    """抽取失败, 消息里要带「怎么办」。"""


# ---------------------------------------------------------- docx
def _w_text(el) -> str:
    """一个 w:p / w:tc 里的可见文字。

    w:t 是文本, w:tab 是制表, w:br / w:cr 是换行 —— 不处理后两者会把
    "甲方:___乙方:___" 挤成一行。
    """
    out = []
    for node in el.iter():
        tag = node.tag.split("}")[-1]
        if tag == "t" and node.text:
            out.append(node.text)
        elif tag == "tab":
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def _docx_style_prefix(para) -> str:
    """标题层级 → markdown 井号。

    w:pStyle 的值形如 Heading1 / 1 / a3(中文模板常见)。只认数字结尾的 HeadingN,
    其余样式名不猜 —— 猜错会把正文标成标题, 比不标更糟。
    """
    ppr = para.find("w:pPr", NS)
    if ppr is None:
        return ""
    st = ppr.find("w:pStyle", NS)
    if st is None:
        return ""
    v = (st.get(f"{{{NS['w']}}}val") or "")
    m = re.match(r"(?:Heading|heading|\u6807\u9898)\s*(\d)", v)
    if m:
        return "#" * min(6, int(m.group(1))) + " "
    return ""


def from_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "word/document.xml" not in names:
            raise ExtractError(
                f"{path.name} 不是有效的 docx (缺 word/document.xml)。"
                "若是老的 .doc 二进制格式, 先转一下: "
                "libreoffice --headless --convert-to docx <文件>")
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find("w:body", NS)
    if body is None:
        return ""
    out: list[str] = []
    # 直接遍历 body 的**直接子节点**: 段落与表格是平级的, 用 iter() 会把
    # 表格里的段落再抽一遍 → 内容翻倍。
    for child in body:
        tag = child.tag.split("}")[-1]
        if tag == "p":
            t = _w_text(child).strip()
            if t:
                out.append(_docx_style_prefix(child) + t)
        elif tag == "tbl":
            rows = []
            for tr in child.findall("w:tr", NS):
                cells = [re.sub(r"\s*\n\s*", " ", _w_text(tc)).strip()
                         for tc in tr.findall("w:tc", NS)]
                if any(cells):
                    rows.append("| " + " | ".join(cells) + " |")
            if rows:
                out.append("")
                out.extend(rows)
                out.append("")
    return "\n".join(out)


# ----------------------------------------------------------- xlsx
def _col_letters(ref: str) -> str:
    return "".join(c for c in ref if c.isalpha())


def _col_idx(ref: str) -> int:
    """A→0, B→1 … AA→26。稀疏行要按列号补空, 否则 TSV 列会串位。"""
    n = 0
    for c in _col_letters(ref).upper():
        n = n * 26 + (ord(c) - 64)
    return n - 1


def from_xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "xl/workbook.xml" not in names:
            raise ExtractError(f"{path.name} 不是有效的 xlsx")
        # 共享字符串表: 单元格里的文本大多不在 sheet 里, 而是指向这张表的下标
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sst.findall("s:si", NS):
                shared.append("".join(t.text or "" for t in si.iter()
                                              if t.tag.split("}")[-1] == "t"))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rel_map = {}
        if "xl/_rels/workbook.xml.rels" in names:
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            for rel in rels:
                rel_map[rel.get("Id")] = rel.get("Target")
        sheets = []
        for sh in wb.iter():
            if sh.tag.split("}")[-1] != "sheet":
                continue
            rid = sh.get(f"{{{NS['r']}}}id")
            tgt = rel_map.get(rid, "")
            tgt = tgt[1:] if tgt.startswith("/") else ("xl/" + tgt if tgt else "")
            sheets.append((sh.get("name") or "?", tgt))

        out: list[str] = []
        for name, target in sheets:
            if target not in names:
                continue
            ws = ET.fromstring(z.read(target))
            rows_txt: list[str] = []
            for row in ws.iter():
                if row.tag.split("}")[-1] != "row":
                    continue
                cells: dict[int, str] = {}
                for c in row.findall("s:c", NS):
                    ref = c.get("r") or ""
                    t = c.get("t")
                    v = c.find("s:v", NS)
                    isnode = c.find("s:is", NS)
                    if t == "s" and v is not None and v.text:
                        val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                    elif t == "inlineStr" and isnode is not None:
                        val = "".join(x.text or "" for x in isnode.iter()
                                    if x.tag.split("}")[-1] == "t")
                    else:
                        # 公式单元格取 <v> 的**缓存值**: 算不了公式, 但 Excel 存了上次的结果
                        val = (v.text or "") if v is not None else ""
                    if val:
                        cells[_col_idx(ref) if ref else len(cells)] = \
                            re.sub(r"\s*\n\s*", " ", val).strip()
                if cells:
                    width = max(cells) + 1
                    rows_txt.append("\t".join(cells.get(i, "") for i in range(width)))
            if rows_txt:
                out.append(f"## sheet: {name}  ({len(rows_txt)} 行)")
                out.extend(rows_txt)
                out.append("")
        return "\n".join(out)


# ------------------------------------------------------------------ pptx
def from_pptx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        slides = sorted((n for n in names
                         if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda s: int(re.search(r"(\d+)", s).group(1)))
        if not slides:
            raise ExtractError(f"{path.name} 里没有 slide (不是有效 pptx?)")
        out: list[str] = []
        for i, s in enumerate(slides, 1):
            root = ET.fromstring(z.read(s))
            txt = [t.text.strip() for t in root.iter()
                   if t.tag.split("}")[-1] == "t" and (t.text or "").strip()]
            out.append(f"## 第 {i} 页")
            out.extend(txt)
            # 备注页往往写着讲稿, 对理解内容比标题有用
            nn = s.replace("slides/slide", "notesSlides/notesSlide")
            if nn in names:
                nroot = ET.fromstring(z.read(nn))
                nt = [t.text.strip() for t in nroot.iter()
                      if t.tag.split("}")[-1] == "t" and (t.text or "").strip()]
                # 备注里会混进页码这种纯数字, 滤掉
                nt = [x for x in nt if not x.isdigit()]
                if nt:
                    out.append("[备注] " + " ".join(nt))
            out.append("")
        return "\n".join(out)


# -------------------------------------------------------------- pdf
def from_pdf(path: Path) -> str:
    try:
        import pymupdf as _fz                     # 新名字
    except ImportError:
        try:
            import fitz as _fz                  # 旧名字
        except ImportError:
            _fz = None
    if _fz is not None:
        doc = _fz.open(str(path))
        out = []
        for i, page in enumerate(doc, 1):
            t = page.get_text().strip()
            out.append(f"## 第 {i} 页")
            if t:
                out.append(t)
            out.append("")
        doc.close()
        body = "\n".join(out)
        if len(re.sub(r"\s|#|第|页", "", body)) < 20:
            body += ("\n\n[注意] 几乎没抽到文字 —— 这多半是扫描版 PDF (图片), "
                     "要 OCR 才能读。")
        return body
    import shutil
    import subprocess
    if shutil.which("pdftotext"):
        p = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                           capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            return p.stdout
    raise ExtractError(
        "PDF 需要 pymupdf 或 pdftotext, 两个都没找到。装一个:\n"
        "  pip install pymupdf        # 推荐, 纯 python\n"
        "  apt install poppler-utils  # 提供 pdftotext")


# ------------------------------------------------- 文本类
def from_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------- 派发
def sniff(path: Path) -> str:
    """真实类型, 不只看扩展名。

    飞书上传下来的文件名可能被改过; 而 OOXML 一律以 PK 开头, ZIP 内的目录名
    才是真类型。判错类型会抛一堆 XML 报错, 不如先看一眼。
    """
    head = path.open("rb").read(4)
    if head[:2] == b"PK":
        try:
            with zipfile.ZipFile(path) as z:
                n = z.namelist()
            if any(x.startswith("word/") for x in n):
                return "docx"
            if any(x.startswith("xl/") for x in n):
                return "xlsx"
            if any(x.startswith("ppt/") for x in n):
                return "pptx"
        except zipfile.BadZipFile:
            pass
        return "zip"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"                    # 老的 .doc/.xls/.ppt 二进制
    return "text" if path.suffix.lower() in TEXTY else "unknown"


def extract(path: Path, max_chars: int = MAX_DEFAULT) -> dict:
    """返回 {ok, kind, chars, truncated, text, error}。"""
    if not path.is_file():
        return {"ok": False, "error": f"文件不存在: {path}"}
    kind = sniff(path)
    try:
        if kind == "docx":
            text = from_docx(path)
        elif kind == "xlsx":
            text = from_xlsx(path)
        elif kind == "pptx":
            text = from_pptx(path)
        elif kind == "pdf":
            text = from_pdf(path)
        elif kind == "text":
            text = from_text(path)
        elif kind == "ole":
            raise ExtractError(
                f"{path.name} 是老式 Office 二进制格式 (.doc/.xls/.ppt)。先转换:\n"
                f"  libreoffice --headless --convert-to docx '{path}'")
        elif kind == "zip":
            with zipfile.ZipFile(path) as z:
                names = z.namelist()[:200]
            text = "这是普通 zip 压缩包, 内含:\n" + "\n".join("  " + n for n in names)
        else:
            raise ExtractError(
                f"不认识的类型 (后缀 {path.suffix or '无'})。支持: "
                "docx/xlsx/pptx/pdf + 纯文本类")
    except ExtractError as e:
        return {"ok": False, "kind": kind, "error": str(e)}
    except zipfile.BadZipFile:
        return {"ok": False, "kind": kind, "error": f"{path.name} 压缩包损坏, 可能下载不完整"}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "kind": kind,
                "error": f"{type(e).__name__}: {e}"}

    text = re.sub(r"\n{4,}", "\n\n\n", text.replace("\r\n", "\n")).strip()
    n = len(text)
    cut = n > max_chars
    if cut:
        # 标注原长度: 不说的话 agent 会以为自己读完了整篇
        text = text[:max_chars] + f"\n\n…[已截断: 原文 {n} 字, 只给了前 {max_chars} 字]"
    return {"ok": True, "kind": kind, "chars": n, "truncated": cut, "text": text}


def main() -> int:
    ap = argparse.ArgumentParser(description="办公文档 → 纯文本 (docx/xlsx/pptx/pdf/文本)")
    ap.add_argument("path")
    ap.add_argument("--max-chars", type=int, default=MAX_DEFAULT)
    ap.add_argument("--json", action="store_true", help="输出 JSON (含 kind/chars/truncated)")
    a = ap.parse_args()
    r = extract(Path(a.path).expanduser(), a.max_chars)
    if a.json:
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    if not r.get("ok"):
        print(f"✗ {r.get('error')}", file=sys.stderr)
        return 1
    hdr = f"[{r['kind']}] {a.path} · {r['chars']} 字" + (" · 已截断" if r["truncated"] else "")
    print(hdr)
    print("─" * min(72, len(hdr) + 8))
    print(r["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
