// Shared document furniture. Kept in one place so the two documents that go
// to NexterPay cannot drift apart visually.
const {
  Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat,
} = require("docx");

const NAVY = "1F3864";
const GREY = "595959";
const RULE = "BFBFBF";
const HEAD_BG = "1F3864";
const ALT_BG = "F2F5FA";
const WARN_BG = "FDF3E7";

const numbering = {
  config: [
    { reference: "bullets",
      levels: [{ level: 0, format: LevelFormat.BULLET, text: "•",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 360, hanging: 220 } } } }] },
  ],
};

const run = (p, base = 20) => new TextRun({
  text: p.t, bold: p.b, italics: p.i,
  font: p.code ? "Consolas" : undefined,
  size: p.code ? base - 2 : (p.size || base),
  color: p.code ? NAVY : (p.color || "000000"),
});

const P = (text, o = {}) => new Paragraph({
  spacing: { after: o.after === undefined ? 120 : o.after, line: 276 },
  alignment: o.align,
  children: [new TextRun({
    text, bold: o.bold, italics: o.italics,
    size: o.size || 20, color: o.color || "000000" })],
});

const RICH = (parts, o = {}) => new Paragraph({
  spacing: { after: o.after === undefined ? 120 : o.after, line: 276 },
  children: parts.map((p) => run(p, o.size || 20)),
});

const BULLET = (parts) => new Paragraph({
  numbering: { reference: "bullets", level: 0 },
  spacing: { after: 70, line: 272 },
  children: (Array.isArray(parts) ? parts : [{ t: parts }]).map((p) => run(p, 20)),
});

const H1 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_1,
  spacing: { before: 300, after: 130 },
  children: [new TextRun({ text, bold: true, size: 26, color: NAVY })],
});

const H2 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_2,
  spacing: { before: 200, after: 80 },
  children: [new TextRun({ text, bold: true, size: 22, color: NAVY })],
});

const RULE_P = () => new Paragraph({
  spacing: { before: 40, after: 130 },
  border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: RULE } },
  children: [new TextRun({ text: "", size: 2 })],
});

const cell = (children, width, shade) => new TableCell({
  width: { size: width, type: WidthType.DXA },
  shading: shade ? { type: ShadingType.CLEAR, fill: shade, color: "auto" } : undefined,
  margins: { top: 58, bottom: 58, left: 105, right: 105 },
  children,
});

const headCell = (text, width) => cell(
  [new Paragraph({ spacing: { after: 0 },
    children: [new TextRun({ text, bold: true, size: 18, color: "FFFFFF" })] })],
  width, HEAD_BG);

const bodyCell = (parts, width, shade) => cell(
  [new Paragraph({ spacing: { after: 0, line: 252 },
    children: (Array.isArray(parts) ? parts : [{ t: parts }]).map((p) => run(p, 18)) })],
  width, shade);

const table = (widths, headers, rows, opts = {}) => new Table({
  columnWidths: widths,
  width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA },
  rows: [
    new TableRow({ tableHeader: true,
      children: headers.map((h, i) => headCell(h, widths[i])) }),
    ...rows.map((r, ri) => new TableRow({
      children: r.map((c, i) =>
        bodyCell(c, widths[i], opts.shade || (ri % 2 ? ALT_BG : undefined))),
    })),
  ],
});

module.exports = {
  NAVY, GREY, RULE, HEAD_BG, ALT_BG, WARN_BG,
  numbering, P, RICH, BULLET, H1, H2, RULE_P, table, cell, bodyCell, headCell, run,
};
