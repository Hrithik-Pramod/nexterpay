const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat, PageBreak,
} = require("docx");

const OUT = process.argv[2];

// ---------------------------------------------------------------------------
// Shared furniture
// ---------------------------------------------------------------------------
const NAVY = "1F3864";
const GREY = "595959";
const RULE = "BFBFBF";
const HEAD_BG = "1F3864";
const ALT_BG = "F2F5FA";

const numbering = {
  config: [
    {
      reference: "bullets",
      levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 360, hanging: 220 } } } },
      ],
    },
  ],
};

const P = (text, opts = {}) => new Paragraph({
  spacing: { after: opts.after === undefined ? 120 : opts.after, line: 276 },
  alignment: opts.align,
  indent: opts.indent,
  children: [new TextRun({
    text, bold: opts.bold, italics: opts.italics, size: opts.size || 20,
    color: opts.color || "000000", font: opts.font,
  })],
});

// A paragraph built from parts, so a sentence can carry a code span.
const RICH = (parts, opts = {}) => new Paragraph({
  spacing: { after: opts.after === undefined ? 120 : opts.after, line: 276 },
  indent: opts.indent,
  children: parts.map((p) => new TextRun({
    text: p.t,
    bold: p.b, italics: p.i,
    font: p.code ? "Consolas" : undefined,
    size: p.code ? 18 : (p.size || opts.size || 20),
    color: p.code ? "1F3864" : (p.color || opts.color || "000000"),
  })),
});

const BULLET = (text, opts = {}) => new Paragraph({
  numbering: { reference: "bullets", level: 0 },
  spacing: { after: 80, line: 276 },
  children: [new TextRun({ text, size: 20, bold: opts.bold })],
});

const BULLET_RICH = (parts) => new Paragraph({
  numbering: { reference: "bullets", level: 0 },
  spacing: { after: 80, line: 276 },
  children: parts.map((p) => new TextRun({
    text: p.t, bold: p.b, italics: p.i,
    font: p.code ? "Consolas" : undefined,
    size: p.code ? 18 : 20,
    color: p.code ? "1F3864" : "000000",
  })),
});

const H1 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_1,
  spacing: { before: 320, after: 140 },
  children: [new TextRun({ text, bold: true, size: 26, color: NAVY })],
});

const H2 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_2,
  spacing: { before: 180, after: 80 },
  children: [new TextRun({ text, bold: true, size: 22, color: NAVY })],
});

const RULE_P = () => new Paragraph({
  spacing: { before: 40, after: 120 },
  border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: RULE } },
  children: [new TextRun({ text: "", size: 2 })],
});

// Cells -------------------------------------------------------------------
const cell = (children, width, opts = {}) => new TableCell({
  width: { size: width, type: WidthType.DXA },
  shading: opts.shade
    ? { type: ShadingType.CLEAR, fill: opts.shade, color: "auto" }
    : undefined,
  margins: { top: 56, bottom: 56, left: 100, right: 100 },
  children,
});

const headCell = (text, width) => cell(
  [new Paragraph({
    spacing: { after: 0 },
    children: [new TextRun({ text, bold: true, size: 18, color: "FFFFFF" })],
  })],
  width, { shade: HEAD_BG }
);

// A body cell whose text may contain monospaced parts.
const bodyCell = (parts, width, shade) => cell(
  [new Paragraph({
    spacing: { after: 0, line: 250 },
    children: (Array.isArray(parts) ? parts : [{ t: parts }]).map((p) =>
      new TextRun({
        text: p.t, bold: p.b, italics: p.i,
        font: p.code ? "Consolas" : undefined,
        size: p.code ? 17 : 18,
        color: p.code ? "1F3864" : "000000",
      })),
  })],
  width, { shade }
);

const table = (widths, headers, rows) => new Table({
  columnWidths: widths,
  width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA },
  rows: [
    new TableRow({
      tableHeader: true,
      children: headers.map((h, i) => headCell(h, widths[i])),
    }),
    ...rows.map((r, ri) => new TableRow({
      children: r.map((c, i) =>
        bodyCell(c, widths[i], ri % 2 ? ALT_BG : undefined)),
    })),
  ],
});

// ---------------------------------------------------------------------------
// Document 1 - Business Group Field Test
// ---------------------------------------------------------------------------
const W = [520, 3000, 5180, 700];   // # | type this | what happens | ok

const fieldTest = [
  new Paragraph({
    spacing: { after: 40 },
    children: [new TextRun({
      text: "NexterPay Operations Platform", bold: true, size: 30, color: NAVY })],
  }),
  new Paragraph({
    spacing: { after: 60 },
    children: [new TextRun({
      text: "Business Group — Field Test", size: 26, color: NAVY })],
  }),
  new Paragraph({
    spacing: { after: 140 },
    children: [new TextRun({
      text: "4 September 2026  ·  Ten minutes  ·  No installation, no training. Typed straight into the group, like any other message.",
      size: 18, color: GREY })],
  }),
  RULE_P(),

  P("The Business group is where commercial questions go to NexterPay — pricing, "
    + "new corridors, contract questions. Anything raised there becomes a tracked "
    + "enquiry with its own reference, so it cannot be lost in a scroll-back.",
    { after: 130 }),

  H2("Before you start"),
  BULLET("You need to be a member of the NexterPay Business group on Telegram. "
    + "There is nothing to install."),
  BULLET_RICH([
    { t: "One rule: start with " }, { t: "/np", code: true },
    { t: ". A message typed without it is an ordinary message in an ordinary "
      + "group — nothing is tracked and nobody is notified." }]),
  BULLET_RICH([
    { t: "Capitals do not matter. " }, { t: "/np", code: true }, { t: ", " },
    { t: "/NP", code: true }, { t: " and " }, { t: "/Np", code: true },
    { t: " all work." }]),
  H2("The tests"),
  table(W,
    ["#", "Type this", "What should happen", "OK?"],
    [
      [[{ t: "1" }],
       [{ t: "/np", code: true }],
       [{ t: "A reply asking " }, { t: "“What would you like to discuss?”", i: true },
        { t: " with two buttons underneath: " }, { t: "Commercial Enquiry", b: true },
        { t: " and " }, { t: "My requests", b: true }, { t: "." }],
       [{ t: "" }]],

      [[{ t: "2" }],
       [{ t: "Tap " }, { t: "Commercial Enquiry", b: true }],
       [{ t: "The message box opens with your name already in it, asking for "
          + "details. Type your question and send it." }],
       [{ t: "" }]],

      [[{ t: "3" }],
       [{ t: "—" }],
       [{ t: "“Request " }, { t: "ACME-1035", code: true },
        { t: " has been logged with our Business team. One of the Business team "
          + "will get back to you.”  Write the reference down." }],
       [{ t: "" }]],

      [[{ t: "4" }],
       [{ t: "/npraise we would like pricing for EUR to NGN payouts", code: true }],
       [{ t: "The same acknowledgement, with a new reference. The one-step "
          + "version — no menu, for when you already know what to say." }],
       [{ t: "" }]],

      [[{ t: "5" }],
       [{ t: "Reply to the bot’s acknowledgement and type anything more" }],
       [{ t: "Nothing comes back, and that is correct.", b: true },
        { t: " Your message has been added to that same enquiry and the NexterPay "
          + "team can see it. No second reference is created." }],
       [{ t: "" }]],

      [[{ t: "6" }],
       [{ t: "/nptickets", code: true }, { t: "  (or tap " }, { t: "My requests", b: true }, { t: ")" }],
       [{ t: "A list of everything raised from this group: the reference, what it "
          + "was about, and where it has got to — " },
        { t: "Received", b: true }, { t: ", " }, { t: "In progress", b: true },
        { t: ", " }, { t: "Waiting on you", b: true }, { t: " or " },
        { t: "Resolved", b: true }, { t: "." }],
       [{ t: "" }]],

      [[{ t: "7" }],
       [{ t: "/NPRAISE testing in capitals", code: true }],
       [{ t: "Exactly as it behaves in lower case. If this one does nothing, "
          + "that is a fault worth reporting." }],
       [{ t: "" }]],

      [[{ t: "8" }],
       [{ t: "hello", code: true }, { t: "  — no slash" }],
       [{ t: "Nothing at all. No reply, no enquiry. Ordinary conversation stays "
          + "ordinary conversation." }],
       [{ t: "" }]],

      [[{ t: "9" }],
       [{ t: "/nphelp", code: true }],
       [{ t: "A short list of what you can do here. It answers for the group you "
          + "are standing in, so it never mentions things that do not apply." }],
       [{ t: "" }]],
    ]),

  H2("Two things that look like faults and are not"),

  RICH([{ t: "1.  Replying to us gets nothing back.  ", b: true },
        { t: "Only the first message creates an enquiry and earns an "
           + "acknowledgement. Everything after that is added quietly to the same "
           + "one. Silence there means it worked." }]),

  RICH([{ t: "2.  Nothing announces that an enquiry is finished.  ", b: true },
        { t: "In the Business group this is deliberate: the answer you were given "
           + "is the conclusion, and a separate “this is now closed” "
           + "message would be noise. A finished enquiry shows as " },
        { t: "Resolved", b: true }, { t: " for four weeks and then drops off the "
           + "list. The other departments do send a closing note; Business does not." }]),

  H2("About your reference"),
  RICH([{ t: "You will see something like " }, { t: "ACME-1035", code: true },
         { t: ". NexterPay staff sometimes quote a longer form internally — " },
         { t: "ACME-SPEX-1035", code: true },
         { t: " — when an enquiry has been filed against one of their "
            + "suppliers. Both point at the same enquiry. The short one is yours, "
            + "and it is the one to quote back to us." }]),

  H2("Reporting back"),
  RICH([{ t: "For anything that surprises you, five things make it fixable: " },
        { t: "what you typed exactly, what you expected, what actually happened, "
           + "roughly when, and the reference if there was one.", b: true },
        { t: "  “It did nothing” counts — that is a real category of fault, and "
           + "the time you sent it is enough to find it in the log." }],
       { after: 0 }),
];

// ---------------------------------------------------------------------------
const doc = new Document({
  numbering,
  styles: { default: { document: { run: { font: "Calibri", size: 20 } } } },
  sections: [{
    properties: { page: { margin: { top: 680, right: 760, bottom: 560, left: 760 } } },
    children: fieldTest,
  }],
});

Packer.toBuffer(doc).then((b) => { fs.writeFileSync(OUT, b); console.log("wrote " + OUT); });
