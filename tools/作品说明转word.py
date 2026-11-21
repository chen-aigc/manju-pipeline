# -*- coding: utf-8 -*-
"""作品说明 Markdown 转 Word（单页 A4）。

投递时通常需要 docx 或 pdf，HR 不一定方便看 md。
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn

SRC = Path(r'D:\deepseek文件区\resume\作品说明_AI漫剧管线.md')
DST = Path(r'D:\deepseek文件区\resume\作品说明_AI漫剧管线.docx')

md = SRC.read_text(encoding='utf-8')
lines = md.split('\n')

doc = Document()

# 页面设置：A4，窄边距
sec = doc.sections[0]
sec.page_width = Cm(21.0)
sec.page_height = Cm(29.7)
sec.top_margin = Cm(1.5)
sec.bottom_margin = Cm(1.5)
sec.left_margin = Cm(1.8)
sec.right_margin = Cm(1.8)

# 默认字体
style = doc.styles['Normal']
style.font.name = 'Microsoft YaHei'
style.font.size = Pt(9.5)
style.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
style.paragraph_format.space_after = Pt(2)
style.paragraph_format.line_spacing = 1.15


def add_inline(par, text):
    """处理 **粗体** 和 `代码` 和 <url>。"""
    # 先按 ** 分段
    parts = re.split(r'(\*\*[^*]+\*\*|`[^`]+`|<[^>]+>)', text)
    for seg in parts:
        if not seg:
            continue
        if seg.startswith('**') and seg.endswith('**'):
            r = par.add_run(seg[2:-2])
            r.bold = True
        elif seg.startswith('`') and seg.endswith('`'):
            r = par.add_run(seg[1:-1])
            r.font.name = 'Consolas'
            r.font.size = Pt(8.5)
        elif seg.startswith('<') and seg.endswith('>'):
            r = par.add_run(seg[1:-1])
            r.font.color.rgb = RGBColor(0x1a, 0x5f, 0xb4)
        else:
            par.add_run(seg)


i = 0
n = len(lines)

while i < n:
    line = lines[i].rstrip()

    # 空行
    if not line.strip():
        i += 1
        continue

    # 分隔线
    if line.strip() == '---':
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after = Pt(4)
        i += 1
        continue

    # 标题
    m = re.match(r'^(#{1,4})\s+(.*)$', line)
    if m:
        level = len(m.group(1))
        text = m.group(2)
        p = doc.add_paragraph()
        if level == 1:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(text)
            r.bold = True
            r.font.size = Pt(16)
            p.paragraph_format.space_after = Pt(4)
        elif level == 2:
            r = p.add_run(text)
            r.bold = True
            r.font.size = Pt(12)
            p.paragraph_format.space_before = Pt(8)
            p.paragraph_format.space_after = Pt(3)
            # 下边框
            pPr = p._p.get_or_add_pPr()
            from docx.oxml import OxmlElement
            pBdr = OxmlElement('w:pBdr')
            bottom = OxmlElement('w:bottom')
            bottom.set(qn('w:val'), 'single')
            bottom.set(qn('w:sz'), '6')
            bottom.set(qn('w:color'), '888888')
            pBdr.append(bottom)
            pPr.append(pBdr)
        else:
            r = p.add_run(text)
            r.bold = True
            r.font.size = Pt(11)
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(2)
        i += 1
        continue

    # 表格
    if line.strip().startswith('|'):
        rows_md = []
        while i < n and lines[i].strip().startswith('|'):
            row = lines[i].strip()
            # 跳过分隔行
            if re.match(r'^\|[\s:\-\|]+\|$', row):
                i += 1
                continue
            cells = [c.strip() for c in row.strip('|').split('|')]
            rows_md.append(cells)
            i += 1

        if not rows_md:
            continue

        ncol = max(len(r) for r in rows_md)
        table = doc.add_table(rows=len(rows_md), cols=ncol)
        table.style = 'Table Grid'
        table.alignment = WD_TABLE_ALIGNMENT.CENTER

        for ri, cells in enumerate(rows_md):
            for ci in range(ncol):
                cell = table.cell(ri, ci)
                txt = cells[ci] if ci < len(cells) else ''
                cell.text = ''
                par = cell.paragraphs[0]
                par.paragraph_format.space_after = Pt(0)
                add_inline(par, txt)
                for run in par.runs:
                    run.font.size = Pt(8.5)
                    if ri == 0:
                        run.bold = True
        i += 1
        continue

    # 代码块
    if line.strip().startswith('```'):
        i += 1
        code_lines = []
        while i < n and not lines[i].strip().startswith('```'):
            code_lines.append(lines[i])
            i += 1
        i += 1  # 跳过结束 ```
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Pt(10)
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(4)
        r = p.add_run('\n'.join(code_lines))
        r.font.name = 'Consolas'
        r.font.size = Pt(8)
        continue

    # 列表项
    m = re.match(r'^(\s*)[-*]\s+(.*)$', line)
    if m:
        indent = len(m.group(1))
        text = m.group(2)
        p = doc.add_paragraph(style='List Bullet')
        p.paragraph_format.left_indent = Pt(12 + indent * 10)
        p.paragraph_format.space_after = Pt(1)
        add_inline(p, text)
        i += 1
        continue

    # 数字列表
    m = re.match(r'^(\s*)(\d+)\.\s+(.*)$', line)
    if m:
        text = m.group(3)
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Pt(12)
        p.paragraph_format.space_after = Pt(1)
        add_inline(p, '%s. %s' % (m.group(2), text))
        i += 1
        continue

    # 普通段落
    p = doc.add_paragraph()
    add_inline(p, line.strip())
    i += 1

doc.save(str(DST))
print('生成: %s' % DST)
print('大小: %.1f KB' % (DST.stat().st_size / 1024))
