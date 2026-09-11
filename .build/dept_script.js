const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, PageBreak } = require("docx");
const C = require("./common.js");
const { NAVY, GREY, WARN_BG, P, RICH, BULLET, H1, H2, RULE_P, table } = C;

const DEPT = process.argv[2];          // "Support" | "Business"
const OUT = process.argv[3];

// Everything that differs between the desks, taken from the running code
// rather than from memory - see the transcript captured on 7 September.
//
// `label` is separate from the script's title because the two differ on one
// desk: the Compliance script is called "Compliance", but the department is
// "Compliance and Risk" and that is the name the bot puts in front of a
// client - "logged with our Compliance and Risk Team". Interpolating the
// title would have printed an expectation the bot never produces, which is
// the one thing a test script must never do.
const V = {
  Support: {
    label: "Support",
    opsName: "Support Operations",
    ask: "What do you need help with?",
    button: "Raise Request",
    prompt: "Please describe the issue, including any reference numbers or "
          + "screenshots that would help us investigate.",
    ackTail: "Please reply to this message if you would like to add anything "
           + "further.",
    claim: "Gavin is now looking after your request.",
    example: "card payments failing since this morning",
    ref: "ACME-1042",
    closes: true,
    closeText: [
      "Request ACME-1042 is now resolved.",
      "",
      "What you raised on 07 September:",
      '"Card payments failing since this morning"',
      "",
      "If anything is still outstanding, reply to this message.",
    ],
  },
  Compliance: {
    label: "Compliance and Risk",
    opsName: "Compliance Operations",
    ask: "What do you need help with?",
    button: "Raise Request",
    prompt: "Please describe the issue, including any reference numbers or "
          + "screenshots that would help us investigate.",
    ackTail: "Please reply to this message if you would like to add anything "
           + "further.",
    claim: "Gavin is now looking after your request.",
    example: "please send the KYC pack for the new corridor",
    ref: "ACME-1050",
    closes: true,
    closeText: [
      "Request ACME-1050 is now resolved.",
      "",
      "What you raised on 11 September:",
      '"Please send the KYC pack for the new corridor"',
      "",
      "If anything is still outstanding, reply to this message.",
    ],
  },
  Business: {
    label: "Business",
    opsName: "Business Operations",
    example: "what would pricing look like for EUR to NGN?",
    ref: "ACME-1047",
    ask: "What would you like to discuss?",
    button: "Commercial Enquiry",
    prompt: "Please describe what you would like to discuss. Include as much "
          + "detail as you can, and attach any documents that would help.",
    ackTail: "One of our Business Team will get back to you. Please reply to "
           + "this message if you would like to add anything further.",
    claim: "Our Business Team is looking into your enquiry.",
    closes: false,
    closeText: null,
  },
}[DEPT];

const MONO = (lines) => lines.map((l) => new Paragraph({
  spacing: { after: 0, line: 240 },
  children: [new TextRun({ text: l || " ", font: "Courier New", size: 17, color: NAVY })],
}));

const S = [480, 3500, 5020, 640];
const steps = (rows) => table(S, ["#", "Do this", "What should happen", "OK?"], rows);
const step = (n, doThis, expect) => [
  [{ t: String(n) }],
  Array.isArray(doThis) ? doThis : [{ t: doThis, code: String(doThis).startsWith("/") }],
  Array.isArray(expect) ? expect : [{ t: expect }],
  [{ t: "" }],
];

const title = [
  new Paragraph({ spacing: { after: 40 }, children: [new TextRun({
    text: "NexterPay Operations Platform", bold: true, size: 32, color: NAVY })] }),
  new Paragraph({ spacing: { after: 60 }, children: [new TextRun({
    text: `${DEPT} — Test Script`, size: 26, color: NAVY })] }),
  new Paragraph({ spacing: { after: 150 }, children: [new TextRun({
    text: "11 September 2026  ·  All three groups, about half an hour  ·  "
        + "Current as of today's build",
    size: 18, color: GREY })] }),
  RULE_P(),
];

const body = [
  P(`Three groups: what a client does in TEST — Acme ${DEPT}, what the desk `
    + `does in TEST — ${V.opsName}, and the supplier side in `
    + `TEST — Pexi ${DEPT}. One person can run all three in three windows. `
    + `Tick as you go; if something fails, note it and carry on.`, { after: 120 }),
  RICH([{ t: "One rule for the client side: ", b: true },
        { t: "start with " }, { t: "/np", code: true },
        { t: ". A message typed without it, and not as a reply, is an ordinary "
           + "message in an ordinary group — nothing is tracked and nobody "
           + "is notified. Capitals do not matter." }], { after: 150 }),

  // ================================================================
  H1(`1.  As the client — in TEST — Acme ${DEPT}`),
  steps([
    step(1, "/np", [
      { t: "“" }, { t: V.ask, i: true }, { t: "” with two buttons: " },
      { t: V.button, b: true }, { t: " and " }, { t: "My Requests", b: true },
      { t: "." }]),
    step(2, [{ t: "Tap " }, { t: V.button, b: true }],
      [{ t: "The message box opens with your name in it, and the instruction "
          + "in " }, { t: "bold", b: true }, { t: ": “" },
       { t: V.prompt, i: true }, { t: "”" }]),
    step(3, [{ t: "Type " }, { t: V.example, code: true },
             { t: " and send" }],
      [{ t: "“Request " }, { t: V.ref, code: true },
       { t: ` has been logged with our ${V.label} Team.” Then: “` },
       { t: V.ackTail, i: true }, { t: "” Write the reference down." }]),
    step(4, "/npraise a second one, in one go",
      "Same acknowledgement, new reference, no menu in between."),
    step(5, [{ t: "Reply to the acknowledgement and type anything" }],
      [{ t: "Nothing comes back, and that is correct.", b: true },
       { t: " It has been added to that same request." }]),
    step(6, [{ t: "Reply to an old bot message that is not about a live "
                 + "request" }],
      [{ t: "“We couldn’t match that to one of your requests, so our " },
       { t: V.label, i: false }, { t: " Team has not been notified.” " },
       { t: "It names this desk, so a client with groups on several of "
          + "ours knows which one missed it.", b: true }]),
    step(7, "/nptickets",
      [{ t: "Your requests, ten at a time, open ones first, with statuses in "
          + "plain words: Received, In Progress, Waiting on You, Resolved." }]),
    step(8, [{ t: "Send a screenshot as a reply to the acknowledgement" }],
      "It reaches the desk. Size is not a limit."),
    step(9, "/nphelp",
      "The client list only. It should not name a single staff command."),
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // ================================================================
  H1(`2.  As the desk — in TEST — ${V.opsName}`),
  P("Open the topic for the request raised in step 3. It will be at the top of "
    + "the list with a red light.", { after: 100 }),
  steps([
    step(10, [{ t: "Read the pinned header" }],
      [{ t: "Reference, subject, the client's own words, who raised it, "
          + "client, department, status, priority, owner. Names are tappable." }]),
    step(11, [{ t: "Tap " }, { t: "Claim", b: true }],
      [{ t: "Status becomes In Progress, your name appears as owner, the "
          + "light turns " }, { t: "amber", b: true },
       { t: ", and the button becomes Reassign." }]),
    step(12, [{ t: "Check the client group" }],
      [{ t: "“" }, { t: `${V.ref} — ${V.claim}`, i: true }, { t: "”" }]),
    step(13, [{ t: "Tap " }, { t: "Reply to Client", b: true },
              { t: ", type something, then " }, { t: "Cancel", b: true }],
      [{ t: "A preview appears; Cancel sends nothing. Confirm in the client "
          + "group that nothing arrived." }]),
    step(14, [{ t: "Reply again and " }, { t: "Send to Client", b: true }],
      [{ t: "In the client group: “" },
       { t: `${V.ref} — from Gavin — We’re looking into this now.`, i: true },
       { t: "” Signed with the name on your staff record." }]),
    step("14a", [{ t: "Tap " }, { t: "Reply to Client", b: true },
                 { t: " and attach a screenshot " }, { t: "with no caption", b: true }],
      [{ t: "The preview names the file, then it reaches the client. " },
       { t: "Fixed 11 September — this used to be refused outright, and a "
          + "screenshot with a caption sent the caption and silently dropped "
          + "the picture. Worth testing properly.", b: true }]),
    step("14b", [{ t: "Do the same again " }, { t: "with a caption", b: true }],
      [{ t: "Client gets both the caption and the image. The caption is not "
          + "replaced." }]),
    step(15, "/npnote the client has been chasing since Tuesday",
      [{ t: "Recorded in the topic. " },
       { t: "Now check the client group — this must not be there.", b: true }]),
    step(16, [{ t: "Tap " }, { t: "More", b: true }, { t: " → " },
              { t: "Priority", b: true }, { t: " → " }, { t: "High", b: true }],
      "An exclamation mark appears in the header and in the topic list."),
    step(17, "/np",
      [{ t: "The staff menu. Inside a topic it offers that request's actions; "
          + "in General it offers raising outbound, the workload, and — "
          + "depending on your level — broadcast and setup. " },
       { t: "The buttons do the thing, not tell you a command.", b: true }]),
    step(18, [{ t: "In General, tap " }, { t: "This Desk’s Workload", b: true }],
      "The workload prints. It should not reply with a command to type."),
    step(19, "/nphistory",
      "The full trail: raised, claimed, replies, notes, priority changes."),
  ]),

  // ================================================================
  H1("3.  Closing"),
  steps([
    step(20, [{ t: "Tap " }, { t: "Close", b: true }],
      [{ t: "The light goes " }, { t: "green", b: true },
       { t: ", then the topic is archived — in that order." }]),
    step(21, [{ t: "Check the client group" }],
      V.closes
        ? [{ t: "They are told, with a summary of what they raised:" }]
        : [{ t: "Nothing at all.", b: true },
           { t: " Business closes silently by design — the answer was the "
              + "conclusion, and a closure notice would be noise. Every other "
              + "department does tell them. " },
           { t: "This is the one most often reported as a fault.", b: true }]),
    step(22, "/nptickets in the client group",
      "The request now reads Resolved. It stays visible for four weeks."),
  ]),
  ...(V.closes ? [P("", { after: 90 }), ...MONO(V.closeText)] : []),
  P("", { after: 140 }),

  new Paragraph({ children: [new PageBreak()] }),

  // ================================================================
  H1(`4.  The supplier side — TEST — Pexi ${DEPT}`),
  P("New since the last scripts: every desk now has a supplier group as well "
    + "as a client one. A supplier group works exactly like a client group — "
    + "the difference is the filing, and it is worth seeing the difference "
    + "rather than taking it on trust.", { after: 100 }),
  steps([
    step(23, [{ t: "In " }, { t: `TEST — Pexi ${DEPT}`, b: true },
              { t: ", send " }, { t: "/np", code: true },
              { t: " and raise something" }],
      [{ t: "The reference reads " }, { t: "SPEX-", code: true },
       { t: ", not " }, { t: "ACME-", code: true },
       { t: ". Same flow, different counterparty." }]),
    step(24, [{ t: "In the desk's General, send " },
              { t: "/npnewsu", code: true }],
      [{ t: "Offers " }, { t: `SPEX · TEST — Pexi ${DEPT}`, code: true },
       { t: ". This is us raising something with them, rather than them with "
          + "us. Cancel, or send one and watch it arrive." }]),
    step(25, [{ t: "On the client request from step 3, tap " },
              { t: "More", b: true }, { t: " → " },
              { t: "File under supplier", b: true }, { t: " → Pexi" }],
      [{ t: "The reference becomes " },
       { t: V.ref.replace("ACME-", "ACME-SPEX-"), code: true },
       { t: " on our side. " },
       { t: `Now check the client group: they still see ${V.ref}.`, b: true },
       { t: " A client is never shown which supplier their issue sits with." }]),
    step(26, [{ t: "Reply to the client on that request" }],
      [{ t: "The message still reads " }, { t: V.ref, code: true },
       { t: ". The supplier code never leaves the building." }]),
  ]),
  P("", { after: 100 }),
  RICH([{ t: "Step 25 is the one worth being fussy about. ", b: true },
        { t: "Filing under a supplier is an internal act of bookkeeping. If a "
           + "client ever sees a SPEX reference, that is a leak and I want to "
           + "know the same day." }], { after: 140 }),

  new Paragraph({ children: [new PageBreak()] }),

  // ================================================================
  H1("5.  Things that look like faults and are not"),
  table([3200, 6440], ["What you see", "Why"], [
    [[{ t: "A reply to us gets nothing back" }],
     [{ t: "Only the first message opens a request and earns an "
         + "acknowledgement. Everything after is added quietly. Silence there "
         + "means it worked." }]],
    ...(V.closes ? [] : [[
      [{ t: "Closing tells the client nothing" }],
      [{ t: "Deliberate, in Business only." }],
    ]]),
    [[{ t: "An administrator command in the client group does nothing" }],
     [{ t: "Silent outside NexterPay's own groups on purpose. Explaining our "
         + "internal mechanism to a counterparty would be the fault." }]],
    [[{ t: "A forwarded message does nothing" }],
     [{ t: "The bot acts on a command, a reply to one of its own messages, or "
         + "a message it can match to a request. A forward is none of those. "
         + "Reply instead, and attach whatever helps." }]],
    [[{ t: "“That needs manager on this desk”" }],
     [{ t: "Seniority is held per desk. Being senior elsewhere does not carry "
         + "across; administration does." }]],
    [[{ t: "A closed request shows only two buttons" }],
     [{ t: "Everything else would be refused on a closed item." }]],
    [[{ t: "“That has to be a reply” — when you did reply" }],
     [{ t: "Only when naming a contact with /npsetlead in a client or supplier "
         + "group. The bot is an ordinary member there and only ever receives "
         + "commands, so it cannot resolve a reply to ordinary chat. Ask them "
         + "to send /np, then reply to that." }]],
  ], { shade: WARN_BG }),

  // ================================================================
  H1("6.  Reporting"),
  RICH([{ t: "For anything unexpected: " },
        { t: "what you did, what you expected, what happened, the group, the "
           + "reference, and roughly when.", b: true },
        { t: "  The time is the useful part — the server log can tell the "
           + "difference between the bot ignoring a message and Telegram never "
           + "delivering it, and those look identical from inside the group." }],
       { after: 0 }),
];

const doc = new Document({
  numbering: C.numbering,
  styles: { default: { document: { run: { font: "Calibri", size: 20 } } } },
  sections: [{
    properties: { page: { margin: { top: 760, right: 800, bottom: 700, left: 800 } } },
    children: [...title, ...body],
  }],
});

Packer.toBuffer(doc).then((b) => { fs.writeFileSync(OUT, b); console.log("wrote " + OUT); });
