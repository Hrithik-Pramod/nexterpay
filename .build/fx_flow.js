const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, PageBreak } = require("docx");
const C = require("./common.js");
const { NAVY, GREY, WARN_BG, P, RICH, BULLET, H1, H2, RULE_P, table } = C;

const OUT = process.argv[2];

const MONO = (lines) => lines.map((l) => new Paragraph({
  spacing: { after: 0, line: 240 },
  children: [new TextRun({ text: l || " ", font: "Courier New", size: 17, color: NAVY })],
}));

const title = [
  new Paragraph({ spacing: { after: 40 }, children: [new TextRun({
    text: "NexterPay Operations Platform", bold: true, size: 32, color: NAVY })] }),
  new Paragraph({ spacing: { after: 60 }, children: [new TextRun({
    text: "Finance — the FX Flow", size: 26, color: NAVY })] }),
  new Paragraph({ spacing: { after: 150 }, children: [new TextRun({
    text: "5 September 2026  ·  For Jason, Blockcognitive Ltd  ·  "
        + "Expands section 5 of the Filing Structure note",
    size: 18, color: GREY })] }),
  RULE_P(),
];

const body = [
  P("You asked for my understanding of the FX route written down, so you can "
    + "check whether you have laboured it. Short answer: you have not laboured "
    + "the thinking, but two of the six stages are the same stage, and one more "
    + "is a step that says nothing new. Four states carry everything you "
    + "described.", { after: 120 }),
  P("Where I disagree with thinning is at the end, in section 6 — and there is "
    + "one stage I cannot place at all without an answer from you.",
    { after: 150 }),

  // -----------------------------------------------------------------
  H1("1.  The route, as I have it recorded"),
  P("From your description, carried into the Filing Structure note on "
    + "30 August:", { after: 100 }),
  ...MONO([
    "  approve rate  →  confirm amount  →  approve conversion  →",
    "  chase settlement  →  issue hash  →  close",
    "",
    "  and, where the rate comes back too high:",
    "  a free-format response that returns rather than advances",
  ]),
  P("", { after: 120 }),
  RICH([{ t: "A correction while we are here. ", b: true },
        { t: "The Filing Structure note calls these “the five stages” and then "
           + "lists six. My miscount, on 30 August, and it has been sitting in "
           + "the reference document ever since. If you have been working from "
           + "that line, it is worth knowing the list was right and the number "
           + "was wrong." }], { after: 120 }),
  P("Two observations were recorded at the time and both still hold. Some "
    + "stages carry a value rather than only a state — confirm amount implies "
    + "an amount, issue hash implies a hash — and that is what makes an FX "
    + "order different from a support ticket. And a rejected rate moves "
    + "backwards, which is the only branch in the whole flow.", { after: 120 }),

  // -----------------------------------------------------------------
  H1("2.  The test I applied"),
  RICH([{ t: "A stage earns its place if it changes who you are waiting on.",
          b: true }], { after: 100 }),
  P("A status set exists so that anyone looking at the board can tell, without "
    + "reading the conversation, whose move it is. That is the entire job. If "
    + "two consecutive stages both mean “waiting on the client”, they are one "
    + "state carrying two pieces of information, and splitting them buys a "
    + "second click and an extra way to leave an order in the wrong place.",
    { after: 100 }),
  P("Applying that to your six:", { after: 100 }),

  table([2500, 2100, 5040],
    ["Stage", "Waiting on", "Verdict"],
    [
      [[{ t: "approve rate" }], [{ t: "the client" }],
       [{ t: "A real state." }]],
      [[{ t: "confirm amount" }], [{ t: "the client" }],
       [{ t: "Same party, same decision. A rate " }, { t: "is", i: true },
        { t: " a quote for an amount — change the amount and the rate changes "
           + "— so these were never independently confirmable." }]],
      [[{ t: "approve conversion" }], [{ t: "unclear", b: true }],
       [{ t: "See section 5. This is the one I cannot place." }]],
      [[{ t: "chase settlement" }], [{ t: "the supplier" }],
       [{ t: "A real state — though “chase” is the action you take while in "
           + "it. The state is " }, { t: "awaiting settlement", i: true },
        { t: "." }]],
      [[{ t: "issue hash" }], [{ t: "us" }],
       [{ t: "A real state, and it is what finishes the order." }]],
      [[{ t: "close" }], [{ t: "nobody" }],
       [{ t: "Nothing happens between issuing the hash and closing. A second "
           + "click that records no new fact." }]],
    ]),

  new Paragraph({ children: [new PageBreak()] }),

  // -----------------------------------------------------------------
  H1("3.  What I recommend"),
  P("Four states and one return path.", { after: 110 }),
  ...MONO([
    "    RATE QUOTED             waiting on the client",
    "        |",
    "        |   they accept",
    "        v",
    "    CONFIRMED               waiting on us",
    "        |",
    "        |   sent to the supplier",
    "        v",
    "    AWAITING SETTLEMENT     waiting on the supplier",
    "        |",
    "        |   hash captured",
    "        v",
    "    SETTLED                 the order is closed",
    "",
    "",
    "    and the one branch:",
    "",
    "    RATE QUOTED --> too high --> RATE REJECTED --> RATE QUOTED",
  ]),
  P("", { after: 130 }),

  H2("What each state holds"),
  table([2300, 2200, 5140],
    ["State", "Waiting on", "What it records"],
    [
      [[{ t: "Rate quoted", b: true }], [{ t: "Client" }],
       [{ t: "The rate, the amount, and the currency pair. Quoted together "
           + "because they are one offer." }]],
      [[{ t: "Confirmed", b: true }], [{ t: "NexterPay" }],
       [{ t: "The figures the client accepted, fixed at the moment of "
           + "acceptance. Nothing above this line can change afterwards "
           + "without a new quote." }]],
      [[{ t: "Awaiting settlement", b: true }], [{ t: "Supplier" }],
       [{ t: "When it was sent to the supplier, and to whom. Chasing happens "
           + "in here and is recorded as messages, not as states." }]],
      [[{ t: "Settled", b: true }], [{ t: "Nobody" }],
       [{ t: "The transaction hash. Capturing it is what closes the order — "
           + "there is no separate close." }]],
      [[{ t: "Rate rejected", b: true }], [{ t: "NexterPay" }],
       [{ t: "The client's own words on why. Returns to Rate quoted, so the "
           + "next quote is a new offer on the same order rather than a new "
           + "order." }]],
    ]),

  H2("Your six, mapped onto the four"),
  table([3200, 6440],
    ["You said", "It becomes"],
    [
      [[{ t: "approve rate" }, { t: " + " }, { t: "confirm amount" }],
       [{ t: "Rate quoted", b: true },
        { t: ", carrying both. One decision by one party." }]],
      [[{ t: "approve conversion" }],
       [{ t: "Unresolved — see section 5. Either a control that stays, or the "
           + "step to drop." }]],
      [[{ t: "chase settlement" }],
       [{ t: "Awaiting settlement", b: true },
        { t: ". Chasing is what you do in the state, not a state of its own." }]],
      [[{ t: "issue hash" }, { t: " + " }, { t: "close" }],
       [{ t: "Settled", b: true },
        { t: ". The hash is the conclusion; nothing follows it." }]],
      [[{ t: "free-format rejection" }],
       [{ t: "Rate rejected", b: true },
        { t: ", the one return path, with a defined destination." }]],
    ]),

  new Paragraph({ children: [new PageBreak()] }),

  // -----------------------------------------------------------------
  H1("4.  What I would not thin"),
  P("Three things earn their cost, and taking any of them out leaves you with "
    + "a support ticket wearing different status names.", { after: 110 }),

  table([3000, 6640],
    ["Keep", "Why"],
    [
      [[{ t: "The captured values" }],
       [{ t: "Rate and amount at the top, hash at the bottom. These are the "
           + "whole difference between an FX order and a conversation about "
           + "one. Without them the record cannot answer “what was agreed?” "
           + "six weeks later, which is the question that will actually be "
           + "asked." }]],
      [[{ t: "The rejected-rate return path" }],
       [{ t: "It is the only branch in the flow and it is the case that "
           + "happens most often. A flow with no way back gets worked around, "
           + "and the workaround is somebody closing the order and opening a "
           + "new one — which loses the link between the two quotes." }]],
      [[{ t: "Sequence enforcement" }],
       [{ t: "A hash should not be issuable before a rate is approved. Our "
           + "recommendation remains yes. The cost is real and worth naming: "
           + "a stage cannot be skipped when events overtake the process, so "
           + "somebody who settles on a phone call has to record the steps "
           + "afterwards rather than jumping to the end." }]],
    ], { shade: WARN_BG }),

  // -----------------------------------------------------------------
  H1("5.  The question I cannot answer"),
  RICH([{ t: "Approve conversion — who approves, and what?", b: true }],
       { after: 90 }),
  P("Two readings, and they lead to opposite conclusions:", { after: 100 }),
  BULLET([{ t: "NexterPay signing off internally before executing. ", b: true },
          { t: "Then it is a control, it stays, and it is separate from "
             + "anything the client does — a second pair of eyes before money "
             + "moves. It would sit between Confirmed and Awaiting settlement, "
             + "making five states rather than four." }]),
  BULLET([{ t: "The client confirming a third time. ", b: true },
          { t: "Then it is the step to drop. They already made that decision "
             + "when they accepted the rate, and asking again invites them to "
             + "reconsider a price that has since moved." }]),
  P("One sentence from you settles it, and it is the only thing standing "
    + "between this and a finished specification.", { after: 120 }),

  // -----------------------------------------------------------------
  H1("6.  What this depends on, and in what order"),
  P("Worth being direct, because it affects where it is worth spending effort.",
    { after: 110 }),

  ...MONO([
    "  1.  Two-sided tickets  (the bridge)      <-- not started",
    "  2.  FX states and captured values        <-- this document",
  ]),
  P("", { after: 120 }),

  P("An FX order runs between your client and your supplier with NexterPay in "
    + "the middle. That is a two-sided ticket, and it does not exist yet. "
    + "Section 4 of the Filing Structure note sets out what it needs: one order "
    + "visible in one Operations topic, with the client seeing only their half "
    + "and the supplier only theirs, and nothing crossing between them without "
    + "somebody deciding it should.", { after: 110 }),

  RICH([{ t: "The consequence for your question: ", b: true },
        { t: "thinning the stages will not save you much. Six states versus "
           + "four is a smaller piece of work than it appears — a status set "
           + "with two extra fields. The bridge underneath is the expensive "
           + "half, and it is expensive because of what it removes." }],
       { after: 110 }),

  P("Today the platform can only send a message back to the group a request "
    + "came from. Sending to the wrong party is impossible by construction — "
    + "there is no code path that could do it. A two-sided ticket deletes that "
    + "guarantee and replaces it with a rule that has to be enforced everywhere "
    + "and tested from every angle. That is the work, and it is the reason we "
    + "would rather build it carefully than quickly.", { after: 110 }),

  RICH([{ t: "So: trim the stages because a shorter flow is a better flow to "
             + "work in, not because it will shorten the build." }],
       { after: 130 }),

  // -----------------------------------------------------------------
  H1("7.  Where this leaves us"),
  table([3000, 6640],
    ["", "Status"],
    [
      [[{ t: "The four states" }],
       [{ t: "Proposed here. Yours to accept, amend or reject." }]],
      [[{ t: "Captured values" }],
       [{ t: "Rate, amount, currency pair, hash. Agreed in principle on "
           + "30 August." }]],
      [[{ t: "Rejected rate returns" }],
       [{ t: "Agreed. Destination now defined as Rate quoted." }]],
      [[{ t: "Sequence enforced" }],
       [{ t: "Recommended, with the cost stated. Awaiting your decision." }]],
      [[{ t: "Approve conversion" }],
       [{ t: "Open. One sentence from you closes it.", b: true }]],
      [[{ t: "Two-sided tickets" }],
       [{ t: "Specified, not started, not quoted. FX cannot begin before it." }]],
      [[{ t: "Finance status wording" }],
       [{ t: "Open. The names above are mine; if your team calls them "
           + "something else, use theirs — these are read by people under "
           + "time pressure." }]],
    ]),

  P("Answer section 5 and I will write the final version into the Filing "
    + "Structure note, so there is one agreed reference rather than a thread "
    + "of messages.", { after: 0, italics: true, color: GREY }),
];

const doc = new Document({
  numbering: C.numbering,
  styles: { default: { document: { run: { font: "Calibri", size: 20 } } } },
  sections: [{
    properties: { page: { margin: { top: 780, right: 820, bottom: 700, left: 820 } } },
    children: [...title, ...body],
  }],
});

Packer.toBuffer(doc).then((b) => { fs.writeFileSync(OUT, b); console.log("wrote " + OUT); });
