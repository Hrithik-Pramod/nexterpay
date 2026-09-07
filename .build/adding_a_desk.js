const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, PageBreak } = require("docx");
const C = require("./common.js");
const { NAVY, GREY, WARN_BG, P, RICH, BULLET, H1, H2, RULE_P, table } = C;

const OUT = process.argv[2];

const MONO = (lines) => lines.map((l) => new Paragraph({
  spacing: { after: 0, line: 240 },
  children: [new TextRun({ text: l || " ", font: "Courier New", size: 17, color: NAVY })],
}));

const S = [480, 4000, 4520, 640];
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
    text: "Adding a Desk — Setup Guide", size: 26, color: NAVY })] }),
  new Paragraph({ spacing: { after: 150 }, children: [new TextRun({
    text: "7 September 2026  ·  One Operations Group, one client, one supplier  ·  "
        + "About fifteen minutes per desk",
    size: 18, color: GREY })] }),
  RULE_P(),
];

const body = [
  P("Written for the four groups asked for on 7 September — a Development "
    + "desk with a client and a supplier, and a supplier for Business — but "
    + "the steps are the same for any desk, so keep this for the next one.",
    { after: 120 }),
  RICH([{ t: "Development needs no configuring to “mirror Support”. ", b: true },
        { t: "Business is the only desk in the platform with wording of its "
           + "own; every other desk — Support, Finance, Compliance and Risk, "
           + "Development — already behaves identically. Register it and it "
           + "matches Support exactly." }], { after: 150 }),

  // ================================================================
  H1("1.  The order that matters"),
  P("Two things must be true before you register anything, and both are "
    + "awkward to fix afterwards.", { after: 90 }),
  BULLET([{ t: "Topics ON in an Operations Group, before you register it.", b: true },
          { t: " Requests cannot be created without topics, and the bot needs "
             + "Manage Topics to open them." }]),
  BULLET([{ t: "Topics OFF in a client or supplier group.", b: true },
          { t: " They get one plain conversation. Topics there means their "
             + "messages carry a thread the bot does not track, and replies "
             + "land wherever Telegram decides rather than where they are "
             + "looking." }]),
  P("", { after: 100 }),
  RICH([{ t: "Everything below can be done from buttons instead: send " },
        { t: "/npsetup", code: true },
        { t: " in the new group and it offers Our own Operations Group, A "
           + "client's group, or A supplier's group. The commands are written "
           + "out here because they are quicker once you have done it once, "
           + "and because they say exactly what happened." }], { after: 140 }),

  // ================================================================
  H1("2.  The Development Operations Group"),
  P("Your own group. Staff only — no client or supplier is ever added to it.",
    { after: 90 }),
  steps([
    step(1, [{ t: "Create a Telegram group, named " },
             { t: "Development Operations", b: true }],
      "Any name works; this one matches the four you already have."),
    step(2, [{ t: "Group Settings → " }, { t: "Topics", b: true }, { t: " → Enable" }],
      [{ t: "Before the bot is added, not after. " },
       { t: "This is the step most often missed.", b: true }]),
    step(3, [{ t: "Add the bot, then promote it to administrator with " },
             { t: "Manage Topics", b: true }],
      "Manage Topics is a separate switch inside the admin rights screen."),
    step(4, "/npregisterops development",
      [{ t: "“Registered this group as Development Operations.”" }]),
    step(5, [{ t: "Reply to a message from each team member with " },
             { t: "/npadduser <level> development", code: true }],
      [{ t: "Levels: " }, { t: "operator", code: true }, { t: ", " },
       { t: "senior_operator", code: true }, { t: ", " }, { t: "manager", code: true },
       { t: ", " }, { t: "administrator", code: true },
       { t: ". A second desk does not remove somebody's first." }]),
    step(6, [{ t: "Reply to your own message with " },
             { t: "/npadduser administrator development", code: true }],
      "So you can register the client and supplier groups from inside them."),
    step(7, "/npwhoami",
      "Confirms your desks and your level on each."),
  ]),

  // ================================================================
  H1("3.  A client group on Development"),
  steps([
    step(8, [{ t: "Create the group — " }, { t: "leave Topics off", b: true }],
      "An ordinary group, like their existing ones."),
    step(9, [{ t: "Add the bot" }],
      [{ t: "Whether you promote it to administrator is the same choice you "
          + "made for your existing client groups — keep the new ones "
          + "consistent. " }, { t: "Section 6 explains how to check.", b: true }]),
    step(10, [{ t: "/npregisterclient development " },
              { t: "<their exact existing name>", b: true }],
      [{ t: "For example " },
       { t: "/npregisterclient development Acme Payments", code: true },
       { t: ". " },
       { t: "The name must match their other groups character for character.",
         b: true },
       { t: " That is what keeps them on one code — get it wrong and you "
          + "create a second counterparty with a new code." }]),
    step(11, "/npstart",
      "Says what this group is now registered as. Check the code is the one "
      + "they already use before going further."),
    step(12, [{ t: "Reply to a message from their person with " },
              { t: "/npsetlead", code: true }],
      [{ t: "Names them as a contact here. Contacts are per group, so this "
          + "does not carry over from their Support group." }]),
    step(13, "/npleads", "Lists who is named."),
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // ================================================================
  H1("4.  A supplier group on Development, and one on Business"),
  P("Identical to a client group but for one word in one command.",
    { after: 90 }),
  steps([
    step(14, [{ t: "Create each group, Topics off, add the bot" }],
      "Two groups: one for Development, one for Business."),
    step(15, [{ t: "In the Development one: " },
              { t: "/npregistersupplier development <exact supplier name>", code: true }],
      [{ t: "Same rule on the name — reuse the supplier's existing one and "
          + "they keep their code." }]),
    step(16, [{ t: "In the Business one: " },
              { t: "/npregistersupplier business <exact supplier name>", code: true }],
      [{ t: "Note this group will use " }, { t: "Business wording", b: true },
       { t: " — “What would you like to discuss?”, “Commercial Enquiry”, and "
          + "no message when a request is closed. That is by design and it "
          + "applies to suppliers on that desk too." }]),
    step(17, [{ t: "In each, reply to their person with " },
              { t: "/npsetlead", code: true }],
      "Names a contact in that group."),
  ]),
  P("", { after: 110 }),
  RICH([{ t: "If a supplier has no Telegram group at all", b: true },
        { t: ", you do not need one to file work against them: " },
        { t: "/npaddparty PEXI Supplier Pexi", code: true },
        { t: " creates the counterparty on its own, and File under supplier "
           + "will then offer it." }], { after: 140 }),

  // ================================================================
  H1("5.  Checking it worked"),
  P("Do this from the server once all four groups exist.", { after: 90 }),
  ...MONO([
    "cd /root/nexterpay",
    "docker compose exec bot python scripts/preflight.py",
  ]),
  P("", { after: 100 }),
  P("It reports every registered group in turn and names what is wrong rather "
    + "than only that something is. The five things it catches are the five "
    + "that account for nearly every failed first test:", { after: 90 }),
  BULLET("Topics not enabled in an Operations Group"),
  BULLET("The bot not an administrator there, or without Manage Topics"),
  BULLET("Topics switched on in a client group, where they should not be"),
  BULLET([{ t: "Another bot administrating a counterparty group — " },
          { t: "the quiet killer", b: true },
          { t: ", because it can stop a bare command reaching us with no error "
             + "anywhere" }]),
  BULLET("Staff who are anonymous Telegram admins, whose messages arrive from "
         + "the group rather than from them"),
  P("", { after: 130 }),

  // ================================================================
  H1("6.  The one decision to make deliberately"),
  RICH([{ t: "Whether the bot is a plain member or an administrator in a "
             + "client or supplier group decides how much it sees.", b: true },
        { t: " As an ordinary member it receives only commands and replies to "
           + "its own messages. As an administrator it receives every message "
           + "in the group." }], { after: 100 }),
  P("Neither is wrong, but the new groups should match the existing ones or "
    + "the same action will behave differently depending on which group "
    + "somebody is standing in. Preflight prints which it found, per group — "
    + "run it before you add the new groups and copy whatever your current "
    + "client groups say.", { after: 130 }),

  // ================================================================
  H1("7.  Where you end up"),
  table([2600, 3500, 3540], ["Desk", "Client group", "Supplier group"], [
    [[{ t: "Support" }], [{ t: "already set up" }], [{ t: "already set up" }]],
    [[{ t: "Finance" }], [{ t: "already set up" }], [{ t: "already set up" }]],
    [[{ t: "Business" }], [{ t: "already set up" }],
     [{ t: "new — section 4", b: true }]],
    [[{ t: "Compliance and Risk" }], [{ t: "already set up" }],
     [{ t: "already set up" }]],
    [[{ t: "Development" }], [{ t: "new — section 3", b: true }],
     [{ t: "new — section 4", b: true }]],
  ]),
  P("", { after: 100 }),
  P("Worth confirming the four marked “already set up” actually are. Send "
    + "/npstart in each group and it will tell you what it is registered as; "
    + "anything unregistered answers plainly rather than staying silent.",
    { after: 130 }),

  // ================================================================
  H1("8.  Things that look like faults and are not"),
  table([3200, 6440], ["What you see", "Why"], [
    [[{ t: "An administrator command in a client group does nothing" }],
     [{ t: "Silent outside NexterPay's own groups on purpose. Explaining our "
         + "internal mechanism to a counterparty would be the fault, not the "
         + "silence. Send it from an Operations Group and it will answer." }]],
    [[{ t: "The Development desk behaves exactly like Support" }],
     [{ t: "Correct. Business is the only desk with wording of its own." }]],
    [[{ t: "A new supplier group shows a new code" }],
     [{ t: "The name did not match their existing one exactly, so a second "
         + "counterparty was created. Fix it with /npsetcode in that group, or "
         + "re-register with the exact name." }]],
    [[{ t: "The Business supplier group says nothing when a request closes" }],
     [{ t: "Business closes silently, on every group on that desk. By "
         + "design." }]],
    [[{ t: "/npsetlead says nothing" }],
     [{ t: "It has to be sent as a reply to a message from the person you are "
         + "naming. Telegram will not tell a bot who is in a group, so "
         + "pointing at a message is the only way it can learn who somebody "
         + "is." }]],
  ], { shade: WARN_BG }),
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
