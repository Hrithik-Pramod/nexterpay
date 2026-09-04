const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, PageBreak } = require("docx");
const C = require("./common.js");
const { NAVY, GREY, ALT_BG, WARN_BG, P, RICH, BULLET, H1, H2, RULE_P, table } = C;

const OUT = process.argv[2];

// Step tables: # | do this | expect this | OK?
const S = [480, 3560, 4960, 640];
// Two-column reference tables
const R2 = [2200, 7440];
const R3 = [1200, 2600, 5840];
const R4 = [1050, 2200, 1250, 5140];

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
    text: "Test Script", size: 26, color: NAVY })] }),
  new Paragraph({ spacing: { after: 160 }, children: [new TextRun({
    text: "4 September 2026  ·  Replaces the 1 September script  ·  "
        + "Every command name changed in this release",
    size: 18, color: GREY })] }),
  RULE_P(),
];

const body = [
  P("This is the whole platform, in the order it is worth testing. Work down it "
    + "and tick as you go — each part assumes the one before it has passed. If "
    + "something fails, note it and carry on; the parts are deliberately "
    + "independent enough that one fault does not block the rest.", { after: 100 }),
  RICH([{ t: "Handing part of this to someone else? ", b: true },
        { t: "The Business group has its own one-page sheet — " },
        { t: "NexterPay — Business Group Field Test", i: true },
        { t: " — written for people who have never seen the bot and need no "
           + "training. Part 3 here is the same ground in more detail." }],
       { after: 160 }),

  // ---------------------------------------------------------------------
  H1("1.  Codes and references"),
  P("Every counterparty has four letters. The code is what turns a request "
    + "number into a reference that says who it belongs to, and it is set once, "
    + "in that counterparty's own group.", { after: 100 }),

  table(R4,
    ["Code", "Party", "Kind", "Their groups"],
    [
      [[{ t: "ACME", code: true }], [{ t: "Acme Payments" }], [{ t: "Client" }],
       [{ t: "Acme Support · Acme Finance · Acme Business · Acme Compliance" }]],
      [[{ t: "SPEX", code: true }], [{ t: "Supplier Pexi" }], [{ t: "Supplier" }],
       [{ t: "Pexi Finance" }]],
    ]),

  H2("What a reference looks like, and who sees which"),
  table(R3,
    ["Form", "Example", "What it means"],
    [
      [[{ t: "CLIENT-n" }], [{ t: "ACME-1035", code: true }],
       [{ t: "A request Acme raised. This is what " }, { t: "Acme", b: true },
        { t: " sees, always." }]],
      [[{ t: "CLIENT-SUPPLIER-n" }], [{ t: "ACME-SPEX-1035", code: true }],
       [{ t: "The same request, filed against Pexi. Only " }, { t: "staff", b: true },
        { t: " see this form. The client is never shown which supplier their "
           + "issue was filed against — that is not always something you would "
           + "choose to disclose." }]],
      [[{ t: "SUPPLIER-n" }], [{ t: "SPEX-1021", code: true }],
       [{ t: "A request " }, { t: "you", b: true },
        { t: " raised with Pexi, rather than one they raised with you." }]],
      [[{ t: "#n" }], [{ t: "#1000", code: true }],
       [{ t: "Legacy. Raised before codes existed. Present only in the current "
           + "test data and gone the moment it is cleared." }]],
    ]),

  H2("Setting a code"),
  steps([
    step(1, [{ t: "In the counterparty's group: " }, { t: "/npsetcode ACME", code: true }],
      [{ t: "Confirms the code. Four letters, and it applies to that "
          + "counterparty everywhere — not just that one group." }]),
    step(2, [{ t: "/npaddparty PEXI Supplier Pexi", code: true }],
      [{ t: "Registers a counterparty that has no Telegram group at all, so "
          + "requests can still be filed against them." }]),
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // ---------------------------------------------------------------------
  H1("2.  What changed in this release"),
  P("Worth reading before testing, because the first item breaks muscle memory.",
    { after: 100 }),
  BULLET([{ t: "Every command lost its underscore. ", b: true },
          { t: "/np_raise", code: true }, { t: " is now " }, { t: "/npraise", code: true },
          { t: ". Nothing answers to the old names. " }, { t: "/nphelp", code: true },
          { t: " lists the new ones from wherever you are." }]),
  BULLET([{ t: "Capitals no longer matter. ", b: true }, { t: "/NPRAISE", code: true },
          { t: " works. It previously did nothing at all, silently." }]),
  BULLET([{ t: "Traffic light on every topic. ", b: true },
          { t: "Red nobody has taken it, amber someone is on it, green closed." }]),
  BULLET([{ t: "High and Critical carry a mark ", b: true },
          { t: "— one exclamation mark for High, a double one for Critical — in the "
             + "topic list and in the header. Telegram gives bots no font colour at "
             + "all, so red text was not available at any price." }]),
  BULLET([{ t: "The header was rebuilt ", b: true },
          { t: "and the names in it are tappable." }]),
  BULLET([{ t: "Three buttons on screen, ", b: true },
          { t: "with More for the rest." }]),
  BULLET([{ t: "New: ", b: true }, { t: "/nphelp", code: true }, { t: ", " },
          { t: "/npsetup", code: true }, { t: ", group leads, asking another "
             + "department, four weeks of client history, and " },
          { t: "/npnewcl", code: true }, { t: " / " }, { t: "/npnewsu", code: true },
          { t: " in place of one outbound command." }]),
  BULLET([{ t: "Fixed: broadcasting. ", b: true },
          { t: "The reply box was opening for nobody, which is why it appeared "
             + "to do nothing." }]),

  // ---------------------------------------------------------------------
  H1("3.  Client side — in any client group"),
  P("Run this in Acme Support first, then repeat step 1 in Acme Business to see "
    + "the wording change.", { after: 100 }),
  steps([
    step(1, "/np", [
      { t: "“What do you need help with?” with " }, { t: "Raise Request", b: true },
      { t: " and " }, { t: "My requests", b: true },
      { t: ". In the " }, { t: "Business", b: true },
      { t: " group the question is “What would you like to discuss?” and the "
         + "button says " }, { t: "Commercial Enquiry", b: true }, { t: "." }]),
    step(2, [{ t: "Tap " }, { t: "Raise Request", b: true }, { t: ", type details, send" }],
      [{ t: "The reply box opens " }, { t: "with your name in it", b: true },
       { t: ". If it opens for nobody, that is the bug we fixed elsewhere — "
          + "report it." }]),
    step(3, "—", [
      { t: "“Request " }, { t: "ACME-1036", code: true },
      { t: " has been logged with our Support team.” A topic appears in Support "
         + "Operations with a " }, { t: "red", b: true }, { t: " light." }]),
    step(4, "/npraise card payments failing since this morning",
      "Same acknowledgement, new reference, no menu in between."),
    step(5, [{ t: "Send a photo or PDF as a reply to the acknowledgement" }],
      "The file appears in the Operations topic. Size is not a limit — files "
      + "are passed through, never downloaded."),
    step(6, "/nptickets",
      "Everything open, plus anything resolved in the last four weeks. Statuses "
      + "are the client wording — Received, In progress, Waiting on you, Resolved."),
    step(7, [{ t: "Type " }, { t: "hello", code: true }, { t: " with no slash" }],
      "Nothing. No reply, no request, no notification."),
    step(8, "/nphelp",
      "The client list only. It should not mention a single staff command."),
  ]),
  RICH([{ t: "Also accepted, and worth one try each: " },
        { t: "/nprequest", code: true }, { t: " and " },
        { t: "/npenquiry", code: true },
        { t: " do exactly what " }, { t: "/npraise", code: true },
        { t: " does. Three words for the same act, because people reach for "
           + "different ones and being refused over vocabulary is a poor "
           + "first impression. " }, { t: "/npstart", code: true },
        { t: " and " }, { t: "/start", code: true },
        { t: " both report whether the bot is live in this group." }],
       { after: 60, size: 18 }),

  new Paragraph({ children: [new PageBreak()] }),

  // ---------------------------------------------------------------------
  H1("4.  Operator side — inside an Operations Group"),
  P("Everything here happens inside a request's topic. Send commands in the "
    + "topic, not in General.", { after: 100 }),
  steps([
    step(1, [{ t: "Open the topic for " }, { t: "ACME-1036", code: true }],
      [{ t: "A pinned header: reference, subject, the client's own words, who "
          + "raised it, client, department, status, priority, owner. Below it: " },
       { t: "Claim", b: true }, { t: ", " }, { t: "Reply to client", b: true },
       { t: ", " }, { t: "Close", b: true }, { t: ", " }, { t: "More", b: true }]),
    step(2, [{ t: "Tap " }, { t: "Claim", b: true }],
      [{ t: "Status becomes In Progress, your name appears as owner " },
       { t: "and is tappable", b: true },
       { t: ", the button becomes Reassign, and the topic light turns " },
       { t: "amber", b: true }, { t: " in the list." }]),
    step(3, [{ t: "Tap " }, { t: "Reply to client", b: true }, { t: ", type, send" }],
      [{ t: "A preview with " }, { t: "Send to client", b: true }, { t: " and " },
       { t: "Cancel", b: true },
       { t: ". Nothing reaches the client until you tap Send." }]),
    step(4, [{ t: "Tap " }, { t: "Cancel", b: true }, { t: " on a draft" }],
      "Nothing is sent. Confirm in the client group that nothing arrived."),
    step(5, "/npnote the client has been chasing this since Tuesday",
      [{ t: "Recorded in the topic. " },
       { t: "Check the client group: this must not appear there.", b: true }]),
    step(6, [{ t: "Tap " }, { t: "More", b: true }],
      [{ t: "Status, Priority, Note, History, File under supplier, Link ticket, "
          + "Ask another department, and " }, { t: "Less", b: true }]),
    step(7, [{ t: "More → Priority → " }, { t: "High", b: true }],
      [{ t: "An exclamation mark appears in the header " },
       { t: "and in the topic list", b: true },
       { t: ". Set it back to Medium and the mark comes off again." }]),
    step(8, [{ t: "More → " }, { t: "File under supplier", b: true }, { t: " → Pexi" }],
      [{ t: "The staff reference becomes " }, { t: "ACME-SPEX-1036", code: true },
       { t: ". Check the client group — they must still see " },
       { t: "ACME-1036", code: true }, { t: "." }]),
    step(9, "/npreply we have raised this with the card scheme",
      "The typed form of the Reply button, sent immediately without a preview. "
      + "Faster when you are certain; the button is safer when you are not."),
    step(10, [{ t: "Reply to a colleague's message with " },
              { t: "/npassign", code: true }],
      "Hands the request to them. Senior Operator and above."),
    step(11, [{ t: "/nplink ACME-1035", code: true }],
      "The typed form of Link ticket, when you already know the reference."),
    step(12, "/nphistory",
      "The full trail: raised, claimed, replies, notes, status changes, with "
      + "who and when."),
    step(13, "/npworkload",
      "Every open request on this desk with its owner and status."),
    step(14, [{ t: "Tap " }, { t: "Close", b: true }],
      [{ t: "Light goes " }, { t: "green", b: true },
       { t: ", then the topic is archived — in that order. The client is told, " },
       { t: "except in Business", b: true },
       { t: ", where closing is silent by design." }]),
    step(15, [{ t: "Client replies to a closed request" }],
      "It reopens nothing and changes no status — but whoever closed it is "
      + "pinged, so it is not missed."),
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // ---------------------------------------------------------------------
  H1("5.  The new features"),

  H2("Ask another department"),
  P("Replaces transferring a request. The request stays where it is and with "
    + "whom it is; another desk is asked a question and answers back.", { after: 90 }),
  steps([
    step(1, [{ t: "In an open request: More → " }, { t: "Ask another department", b: true }],
      "A list of the other four departments."),
    step(2, [{ t: "Pick " }, { t: "Finance", b: true }, { t: ", type the question" }],
      [{ t: "A confirmation before anything is sent. " },
       { t: "The client must see none of this.", b: true }]),
    step(3, "—",
      "A linked request appears in Finance Operations. Answering it puts the "
      + "answer back in the original topic."),
  ]),

  H2("Group leads"),
  P("Telegram will not tell a bot who is in a group, so the people worth "
    + "tagging are named by hand — the same way staff are, by replying to one of "
    + "their messages.", { after: 90 }),
  steps([
    step(1, [{ t: "In a client group, reply to someone with " },
             { t: "/npsetlead", code: true }],
      "They are recorded as a named contact for that group."),
    step(2, "/npleads", "Everyone named for this group."),
    step(3, [{ t: "Reply to that client from Operations" }],
      [{ t: "The confirmation offers " }, { t: "Send and tag <name>", b: true },
       { t: " as well as plain Send. Tagged, it notifies them personally." }]),
    step(4, [{ t: "Reply to them with " }, { t: "/npremovelead", code: true }],
      "They are no longer offered."),
  ]),

  H2("Connected tickets"),
  steps([
    step(1, [{ t: "More → " }, { t: "Link ticket", b: true }],
      "Recent requests to choose from. Pick one."),
    step(2, "—",
      "Both headers now name the other. Neither client group shows anything."),
    step(3, [{ t: "/npunlink ACME-1035", code: true }], "The link is removed from both."),
  ]),

  H2("Raising outbound"),
  steps([
    step(1, "/npnewcl", "Choose a client group, then type the request. It opens "
      + "in their group and in your Operations topic together."),
    step(2, "/npnewsu", "The same, to a supplier. Two commands rather than one "
      + "with a picker, so you know who you are about to write to before you start."),
    step(3, "—",
      [{ t: "The header should say " }, { t: "we raised this", b: true },
       { t: " rather than naming a client contact." }]),
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // ---------------------------------------------------------------------
  H1("6.  Administration"),
  P("Administrators only. Not limited to one department.", { after: 100 }),
  steps([
    step(1, "/npsetup",
      [{ t: "Two buttons: " }, { t: "Register this group", b: true }, { t: " and " },
       { t: "Add a person", b: true },
       { t: ". These are the two jobs done under time pressure with somebody "
          + "waiting; the rest stay as commands." }]),
    step(2, [{ t: "Register this group → pick a kind → pick a department" }],
      "The group is registered without you typing a single argument."),
    step(3, [{ t: "Reply to somebody → Add a person → role → department" }],
      "They are added to that desk."),
    step(4, "/npadduser operator finance",
      "The typed form, as a reply to one of their messages. Same result."),
    step(5, "/npwhoami",
      [{ t: "Their desks, their role on each, and what each role permits. A "
          + "person can hold different seniority on different desks — it does "
          + "not carry across." }]),
    step(6, "/npremoveuser finance",
      "Takes one desk off somebody. With no department, it takes them all."),
    step(7, [{ t: "/npregisterops support", code: true }],
      "The typed form of registering an Operations Group. There is also "
      + "/npregisterclient and /npregistersupplier for counterparty groups. "
      + "These are the longest names on the platform on purpose — run once per "
      + "group, by one person, where being unmistakable beats being short."),
  ]),

  H2("Broadcasting — the one reported as broken"),
  steps([
    step(1, "/npbroadcast",
      [{ t: "The reply box opens " }, { t: "with your name already in it", b: true },
       { t: ". This is the fix. If it opens blank, or does not open, stop and "
          + "report it." }]),
    step(2, [{ t: "Type the message, send" }],
      [{ t: "A preview and the list of groups it will reach, with " },
       { t: "Send", b: true }, { t: " and " }, { t: "Cancel", b: true },
       { t: ". Nothing has gone anywhere yet." }]),
    step(3, [{ t: "Tap " }, { t: "Send", b: true }],
      "It arrives in each counterparty group. Manager or above only."),
    step(4, [{ t: "A client replies to the broadcast" }],
      "That reply opens a new request in the right department — it is not lost."),
    step(5, [{ t: "Recall it" }],
      "It is withdrawn where Telegram still permits it. Telegram refuses beyond "
      + "48 hours, and the bot will say so rather than pretending."),
  ]),

  // ---------------------------------------------------------------------
  H1("7.  The things that must never happen"),
  P("These matter more than any feature. If any one of them fails, stop and "
    + "report it before continuing.", { after: 100 }),
  table([5600, 4040],
    ["Check", "Why it matters"],
    [
      [[{ t: "An internal note never appears in a client group" }],
       [{ t: "Notes are where staff speak plainly to each other." }]],
      [[{ t: "A draft reply never arrives before Send is tapped" }],
       [{ t: "The preview is the only thing between a half-written thought and "
           + "the client." }]],
      [[{ t: "A client never sees a supplier code" }],
       [{ t: "It would let them work out who you use for what." }]],
      [[{ t: "A message in one client group never reaches another" }],
       [{ t: "Two clients in the same industry are the worst case." }]],
      [[{ t: "Asking another department is invisible to the client" }],
       [{ t: "They asked you, not your Finance desk." }]],
      [[{ t: "Closing in Business sends the client nothing" }],
       [{ t: "The answer is the conclusion. A closure notice is noise." }]],
    ], { shade: WARN_BG }),

  // ---------------------------------------------------------------------
  H1("8.  Reporting"),
  RICH([{ t: "For each fault: " },
        { t: "what you did, what you expected, what happened, the group, the "
           + "reference, and roughly when.", b: true },
        { t: "  The time matters — “it did nothing” is a real and distinct kind "
           + "of fault, and the server log can tell the difference between the "
           + "bot ignoring a message and Telegram never delivering it. Those look "
           + "identical from inside the group and have completely different "
           + "causes." }], { after: 120 }),
  RICH([{ t: "Not a fault: ", b: true },
        { t: "silence after replying to us, no closure notice in Business, and "
           + "the older " }, { t: "#1000", code: true },
        { t: " references in the current test data." }], { after: 0 }),
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
