const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, PageBreak } = require("docx");
const C = require("./common.js");
const { NAVY, GREY, WARN_BG, P, RICH, BULLET, H1, H2, RULE_P, table } = C;

const OUT = process.argv[2];

const MONO = (lines) => lines.map((l) => new Paragraph({
  spacing: { after: 0, line: 240 },
  children: [new TextRun({ text: l || " ", font: "Courier New", size: 17, color: NAVY })],
}));

// Column sets
const CMD = [2500, 5400, 1740];      // command | what | who
const TWO = [2900, 6740];
const THREE = [2000, 2400, 5240];

const title = [
  new Paragraph({ spacing: { after: 40 }, children: [new TextRun({
    text: "NexterPay Operations Platform", bold: true, size: 34, color: NAVY })] }),
  new Paragraph({ spacing: { after: 60 }, children: [new TextRun({
    text: "Reference", size: 28, color: NAVY })] }),
  new Paragraph({ spacing: { after: 150 }, children: [new TextRun({
    text: "5 September 2026  ·  Everything the platform does today  ·  "
        + "Replaces the Command and Button Reference",
    size: 18, color: GREY })] }),
  RULE_P(),
];

const body = [
  P("One document. What the platform is, what every group sees, every command "
    + "and button, who can use each, and the things that look like faults and "
    + "are not. Written from the running code rather than from notes, so where "
    + "this disagrees with an older document, this is right.", { after: 150 }),

  // ------------------------------------------------------------------
  H1("1.  What it is"),
  P("A Telegram bot that turns messages from clients and suppliers into "
    + "tracked requests. Each request gets a reference, a topic in the "
    + "department that owns it, an owner, a status and a complete history. "
    + "Clients talk in their own group and see only their own side; staff work "
    + "in Operations Groups and see everything.", { after: 110 }),
  RICH([{ t: "The rule the whole design rests on: ", b: true },
        { t: "nothing a member of staff writes reaches a counterparty unless "
           + "somebody deliberately sends it. Typing in an Operations topic is "
           + "internal. There is one route outward and it always shows you the "
           + "message and the destination before it goes." }], { after: 150 }),

  // ------------------------------------------------------------------
  H1("2.  The groups"),
  table(TWO, ["Kind", "What happens there"], [
    [[{ t: "Operations Group", b: true }],
     [{ t: "One per department. Every request that department owns appears as "
         + "a topic. Staff only. This is where the work is done." }]],
    [[{ t: "Client group", b: true }],
     [{ t: "One per client per department. The client raises requests here and "
         + "sees replies. They never see another client, another department's "
         + "traffic, or anything internal." }]],
    [[{ t: "Supplier group", b: true }],
     [{ t: "The same, for a supplier. Distinguished so that “issues we "
         + "raised with them” and “issues they raised with us” "
         + "stay separable." }]],
  ]),
  P("", { after: 100 }),
  P("Five departments exist: Support, Finance, Business, Compliance and Risk, "
    + "and Development. A counterparty can have a group on any of them, and "
    + "usually a different contact in each.", { after: 140 }),

  // ------------------------------------------------------------------
  H1("3.  References and codes"),
  P("Every counterparty has four letters, set once in their own group with "
    + "/npsetcode. The code is what makes a reference say who it belongs to.",
    { after: 100 }),
  table(THREE, ["Form", "Example", "What it means"], [
    [[{ t: "CLIENT-n" }], [{ t: "ACME-1042", code: true }],
     [{ t: "A request Acme raised. This is what " }, { t: "Acme", b: true },
      { t: " always sees." }]],
    [[{ t: "CLIENT-SUPPLIER-n" }], [{ t: "ACME-SPEX-1042", code: true }],
     [{ t: "The same request, filed against Pexi. " }, { t: "Staff only", b: true },
      { t: " — a client is never shown which supplier their issue sits "
         + "with." }]],
    [[{ t: "SUPPLIER-n" }], [{ t: "SPEX-1042", code: true }],
     [{ t: "A request " }, { t: "we", b: true }, { t: " raised with Pexi." }]],
    [[{ t: "#n" }], [{ t: "#1000", code: true }],
     [{ t: "Legacy, from before codes existed. Only in current test data." }]],
  ]),

  // ------------------------------------------------------------------
  H1("4.  The topic list"),
  P("Every topic carries a light and, where it applies, an urgency mark. This "
    + "is where triage happens, so it is built to be read at a glance without "
    + "opening anything.", { after: 100 }),
  table(TWO, ["Light", "Means"], [
    [[{ t: "Red", b: true }], [{ t: "Nobody has picked it up." }]],
    [[{ t: "Amber", b: true }],
     [{ t: "Somebody is on it — claimed, in progress, waiting, escalated, "
         + "or finished but not yet closed." }]],
    [[{ t: "Green", b: true }], [{ t: "Closed. Green means closed and nothing else." }]],
  ]),
  P("", { after: 100 }),
  P("High priority adds an exclamation mark, Critical a double one, straight "
    + "after the light. Telegram gives a bot no font colour — the whole "
    + "set of styles available is bold, italic, underline, strikethrough, "
    + "spoiler, code, quote, links and mentions — so urgency is a "
    + "character rather than a colour. A closed request drops its mark: green "
    + "says finished and a mark says drop everything.", { after: 110 }),
  ...MONO([
    "    RED  !!  ACME-1042 . Rate not confirmed, needed before close",
    "    RED  !   ACME-1043 . Card payments failing since this morning",
    "    AMBER    ACME-SPEX-1044 . Settlement not received for INV-2041",
    "    GREEN    ACME-1045 . Do you support EUR to NGN yet?",
  ]),
  P("", { after: 100 }),
  P("The counterparty name is deliberately absent: the code is already in the "
    + "reference, and Telegram truncates a topic name from the right, so those "
    + "characters are better spent on the subject.", { after: 140 }),

  new Paragraph({ children: [new PageBreak()] }),

  // ------------------------------------------------------------------
  H1("5.  The header"),
  P("Pinned at the top of every topic, rewritten whenever anything changes.",
    { after: 100 }),
  ...MONO([
    "    ACME-1042 - Rate not confirmed",
    '    "Rate for 250k EUR/GBP not confirmed, needed before close today."',
    "",
    "    Raised    05 Sep 2026 by Tom Baker (they raised this)",
    "    Client    Acme Payments",
    "    Department  Finance",
    "    Status    In Progress     Priority  ! High",
    "    Owner     peter",
    "    Contact   Gavs D",
    "    Linked    ACME-1036",
  ]),
  P("", { after: 110 }),
  BULLET([{ t: "The counterparty's own words come first", b: true },
          { t: ", in quotes, because that is the thing you actually need and "
             + "a block of fields in one weight reads as a form." }]),
  BULLET([{ t: "Names are tappable. ", b: true },
          { t: "The raiser, the owner and the contact are real mentions, so a "
             + "name is a person you can reach." }]),
  BULLET([{ t: "Direction is spelled out ", b: true },
          { t: "— “they raised this” or “we raised this” "
             + "— because a name alone does not say which way the request "
             + "runs." }]),
  BULLET([{ t: "Contact and Linked appear only when there is something to say." }]),

  // ------------------------------------------------------------------
  H1("6.  What a client or supplier sees"),
  P("Deliberately thin. One command to remember, and ordinary conversation "
    + "after that.", { after: 100 }),
  table(CMD, ["Command", "What it does", "Who"], [
    [[{ t: "/np", code: true }],
     [{ t: "The front door. Offers " }, { t: "Raise Request", b: true },
      { t: " and " }, { t: "My requests", b: true },
      { t: ". In a Business group the first button reads " },
      { t: "Commercial Enquiry", b: true }, { t: " and the question is "
         + "“What would you like to discuss?”" }],
     [{ t: "Anyone in the group" }]],
    [[{ t: "/npraise <details>", code: true }],
     [{ t: "Raises it in one go, no menu. " }, { t: "/nprequest", code: true },
      { t: " and " }, { t: "/npenquiry", code: true },
      { t: " do the same — three words for one act, because people reach "
         + "for different ones." }],
     [{ t: "Anyone in the group" }]],
    [[{ t: "/nptickets", code: true }],
     [{ t: "Everything open, plus anything resolved in the last four weeks. "
         + "Ten at a time, open ones first, each tappable to add to it." }],
     [{ t: "Anyone in the group" }]],
    [[{ t: "/nphelp", code: true }],
     [{ t: "The short client list. Never mentions a staff command." }],
     [{ t: "Anyone in the group" }]],
  ]),
  P("", { after: 100 }),
  H2("Statuses, as a client sees them"),
  P("Staff track nine statuses; a client is shown four, because the internal "
    + "distinctions are not their business and would only prompt questions.",
    { after: 90 }),
  table([2600, 7040], ["Client sees", "Which internal statuses"], [
    [[{ t: "Received" }], [{ t: "Open" }]],
    [[{ t: "In progress" }],
     [{ t: "Claimed, In Progress, Waiting for Internal Team, Waiting for Third "
         + "Party, Escalated" }]],
    [[{ t: "Waiting on you" }], [{ t: "Waiting for Client" }]],
    [[{ t: "Resolved" }], [{ t: "Completed, Closed" }]],
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // ------------------------------------------------------------------
  H1("7.  What staff see — buttons"),
  P("Every request carries an Actions message. Three buttons on screen and a "
    + "More: of nine actions, three carry almost all the traffic, and nine "
    + "buttons is eight things to read past to reach the one you want.",
    { after: 100 }),
  ...MONO([
    "    Claim   |   Reply to client   |   Close",
    "    More",
    "",
    "    ... More opens:",
    "    Status              |  Priority",
    "    Note                |  History",
    "    File under supplier |  Link ticket",
    "    Ask another department",
    "    Less",
  ]),
  P("", { after: 110 }),
  BULLET([{ t: "Claim becomes Reassign ", b: true },
          { t: "once somebody owns it, so the first button is always the one "
             + "about ownership." }]),
  BULLET([{ t: "A closed request shows only History and Reopen. ", b: true },
          { t: "Everything else would be refused, and a button that errors is "
             + "worse than one that is not there." }]),
  BULLET([{ t: "A request opened by Ask another department ", b: true },
          { t: "shows " }, { t: "Answer ACME-1042", b: true },
          { t: " in place of Reply to client. The desk holding the client "
             + "relationship talks to the client; the desk being asked talks "
             + "back to them." }]),

  H2("Previews"),
  P("Nothing leaves a group without being shown first.", { after: 90 }),
  table(TWO, ["Preview", "Buttons"], [
    [[{ t: "Reply to a counterparty" }],
     [{ t: "Send and tag <name> — one per named contact — then Send "
         + "to client, and Cancel" }]],
    [[{ t: "Answer another desk" }], [{ t: "Send to ACME-1042, Cancel" }]],
    [[{ t: "Ask another department" }], [{ t: "Ask Finance, Cancel" }]],
    [[{ t: "Raise outbound" }],
     [{ t: "Send and open, Send and tag <name> per contact, Cancel" }]],
    [[{ t: "Broadcast" }], [{ t: "The groups it will reach, then Send and Cancel" }]],
  ]),

  // ------------------------------------------------------------------
  H1("8.  What staff see — commands"),
  P("All of these work inside an Operations Group. Anything needing a "
    + "particular request is sent inside that request's topic.", { after: 100 }),
  table(CMD, ["Command", "What it does", "Level"], [
    [[{ t: "/npreply <message>", code: true }],
     [{ t: "Sends to the counterparty immediately, no preview. Faster when you "
         + "are certain; the button is safer when you are not." }],
     [{ t: "Operator" }]],
    [[{ t: "/npnote <text>", code: true }],
     [{ t: "An internal note. Never leaves the Operations Group." }],
     [{ t: "Operator" }]],
    [[{ t: "/nphistory", code: true }],
     [{ t: "The full trail: raised, claimed, replies, notes, status and "
         + "priority changes, links, answers, closure — who and when." }],
     [{ t: "Operator" }]],
    [[{ t: "/nplink <ref>", code: true }, { t: " / " }, { t: "/npunlink <ref>", code: true }],
     [{ t: "Tie two requests together, or undo it. Any of ACME-SPEX-1042, "
         + "ACME-1042 or 1042 is accepted." }],
     [{ t: "Operator" }]],
    [[{ t: "/npworkload", code: true }],
     [{ t: "Every open request on this desk with owner, status and priority." }],
     [{ t: "Operator" }]],
    [[{ t: "/npnewcl", code: true }, { t: " / " }, { t: "/npnewsu", code: true }],
     [{ t: "Open a request with a client, or with a supplier. Two commands "
         + "rather than one with a picker, so you know who you are writing to "
         + "before you start." }],
     [{ t: "Operator" }]],
    [[{ t: "/npwhoami", code: true }],
     [{ t: "Your desks, your level on each, and what each permits." }],
     [{ t: "Operator" }]],
    [[{ t: "/nprole", code: true }],
     [{ t: "This whole ladder, in the group. Generated from the permission "
         + "checks, so it cannot drift from what the bot actually does." }],
     [{ t: "Operator" }]],
    [[{ t: "/npassign", code: true }],
     [{ t: "Hand a request to somebody — as a reply to one of their "
         + "messages." }],
     [{ t: "Senior Operator" }]],
    [[{ t: "/npbroadcast", code: true }],
     [{ t: "One message to many counterparty groups, previewed with the list "
         + "of recipients. A reply to it opens a new request rather than "
         + "vanishing." }],
     [{ t: "Manager" }]],
    [[{ t: "/nphelp", code: true }],
     [{ t: "What you can do, from where you are standing — filtered by "
         + "the group and by your level on that desk." }],
     [{ t: "Anyone" }]],
    [[{ t: "/start", code: true }, { t: " / " }, { t: "/npstart", code: true }],
     [{ t: "Reports whether the bot is running and what this group is "
         + "registered as. /start is unprefixed because Telegram's own "
         + "interface sends it when somebody taps Start." }],
     [{ t: "Anyone, anywhere" }]],
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // ------------------------------------------------------------------
  H1("9.  The levels"),
  P("Four, each including everything below it.", { after: 100 }),
  table([2400, 7240], ["Level", "Adds"], [
    [[{ t: "Operator", b: true }],
     [{ t: "Claim, reply, note, set status, set priority up to Critical, "
         + "close, file under a supplier, link and unlink, ask another "
         + "department and answer one, raise outbound, see workload and "
         + "history." }]],
    [[{ t: "Senior Operator", b: true }],
     [{ t: "Reassign a request. Escalate." }]],
    [[{ t: "Manager", b: true }],
     [{ t: "Reopen a closed request. Broadcast." }]],
    [[{ t: "Administrator", b: true }],
     [{ t: "Register groups, add and remove staff and set their level, set "
         + "counterparty codes, add counterparties with no group, name and "
         + "remove contacts." }]],
  ]),
  P("", { after: 110 }),
  RICH([{ t: "Seniority is held per desk. ", b: true },
        { t: "Being a Manager in Support does not make you a Manager in "
           + "Finance. Somebody working two desks can hold a different level "
           + "on each, and the bot applies the level for the desk they are "
           + "standing in." }], { after: 90 }),
  RICH([{ t: "Administration is not. ", b: true },
        { t: "Registering groups, managing staff and setting codes work in any "
           + "group, whichever desks that person is on — setting up a "
           + "department you do not work on is exactly what it is for." }],
       { after: 140 }),

  // ------------------------------------------------------------------
  H1("10.  Named contacts"),
  P("Telegram will not tell a bot who is in a group — there is no API "
    + "call for it. So the people worth reaching are named by hand, by "
    + "replying to something they wrote.", { after: 100 }),
  table(CMD, ["Command", "What it does", "Who"], [
    [[{ t: "/npsetlead", code: true }],
     [{ t: "Names somebody, as a reply to one of their messages, in their own "
         + "group." }],
     [{ t: "Administrator, or an existing contact" }]],
    [[{ t: "/npleads", code: true }],
     [{ t: "Who is named. Sent inside a request's topic it answers about that "
         + "request's counterparty." }],
     [{ t: "Administrator, or an existing contact" }]],
    [[{ t: "/npremovelead", code: true }],
     [{ t: "Unnames somebody, as a reply. The last one cannot be removed." }],
     [{ t: "Administrator, or an existing contact" }]],
  ]),
  P("", { after: 100 }),
  BULLET([{ t: "Contacts are per group, not per client. ", b: true },
          { t: "A client with a Support group and a Finance group usually has "
             + "a different person in each." }]),
  BULLET([{ t: "Once named, a contact can name others ", b: true },
          { t: "in that group and nowhere else, so a counterparty can keep "
             + "their own list current without NexterPay rejoining." }]),
  BULLET([{ t: "The list can never be emptied. ", b: true },
          { t: "Contacts are appointed by reply, so removing the last one "
             + "would leave nobody able to name a successor." }]),
  BULLET([{ t: "Everyone else gets silence. ", b: true },
          { t: "A counterparty typing an administrator command in their own "
             + "group receives nothing at all — explaining our internal "
             + "mechanism to them would be the fault, not the silence." }]),

  new Paragraph({ children: [new PageBreak()] }),

  // ------------------------------------------------------------------
  H1("11.  Administration"),
  P("/npsetup is the front door and offers what fits where you are standing.",
    { after: 100 }),
  table(TWO, ["Sent in", "Offers"], [
    [[{ t: "An unregistered group" }],
     [{ t: "Our own Operations Group  ·  A client's group  ·  A "
         + "supplier's group" }]],
    [[{ t: "An Operations Group" }],
     [{ t: "Add a person — then a department, then a level, then the "
         + "command to send as a reply to them" }]],
    [[{ t: "A counterparty group" }], [{ t: "Name a contact here" }]],
  ]),
  P("", { after: 100 }),
  P("A group that is already registered is not offered registration again. "
    + "Not because it would fail — because it would succeed, and "
    + "re-pointing a live client group at another department by mis-tap is "
    + "worse than having to type the command deliberately.", { after: 100 }),
  table(CMD, ["Command", "What it does", "Level"], [
    [[{ t: "/npregisterops <dept>", code: true }],
     [{ t: "Registers this group as an Operations Group. Topics must be on and "
         + "the bot needs Manage Topics." }], [{ t: "Administrator" }]],
    [[{ t: "/npregisterclient <dept> <name>", code: true }],
     [{ t: "Registers a client group. Use the identical name if they already "
         + "have a group with us — that is what keeps them on one code." }],
     [{ t: "Administrator" }]],
    [[{ t: "/npregistersupplier <dept> <name>", code: true }],
     [{ t: "The same, for a supplier." }], [{ t: "Administrator" }]],
    [[{ t: "/npadduser <level> <dept>", code: true }],
     [{ t: "Adds somebody to a desk, as a reply to one of their messages. "
         + "Adding a second desk does not remove the first." }],
     [{ t: "Administrator" }]],
    [[{ t: "/npremoveuser [dept]", code: true }],
     [{ t: "Takes one desk off somebody. With no department, all of them." }],
     [{ t: "Administrator" }]],
    [[{ t: "/npsetcode <CODE>", code: true }],
     [{ t: "Four letters for a counterparty, in their group. Applies "
         + "everywhere they have a group." }], [{ t: "Administrator" }]],
    [[{ t: "/npaddparty <CODE> <name>", code: true }],
     [{ t: "A counterparty with no Telegram group, so requests can still be "
         + "filed against them." }], [{ t: "Administrator" }]],
  ]),

  // ------------------------------------------------------------------
  H1("12.  Things that look like faults and are not"),
  table(TWO, ["What you see", "Why"], [
    [[{ t: "A counterparty replies and gets nothing back" }],
     [{ t: "Only the first message opens a request and earns an "
         + "acknowledgement. Everything after is added quietly to the same "
         + "one. Silence there means it worked." }]],
    [[{ t: "Closing in Business tells the client nothing" }],
     [{ t: "Deliberate. The answer was the conclusion; a separate closure "
         + "notice would be noise. Every other department does tell them." }]],
    [[{ t: "A command in a client group does nothing at all" }],
     [{ t: "Administrator commands are silent outside NexterPay's own groups. "
         + "Explaining our internal mechanism to a counterparty would be the "
         + "fault." }]],
    [[{ t: "“That needs manager on this desk”" }],
     [{ t: "Seniority is per desk. Being an administrator elsewhere does not "
         + "carry across — administration does, seniority does not." }]],
    [[{ t: "A closed request shows only two buttons" }],
     [{ t: "Everything else would be refused on a closed item." }]],
    [[{ t: "A client's reply to a closed request does not reopen it" }],
     [{ t: "Whoever closed it is notified and decides. The client is told it "
         + "is already closed rather than left wondering." }]],
    [[{ t: "A forwarded message does nothing" }],
     [{ t: "The bot runs with privacy mode on and is not an administrator in "
         + "counterparty groups, so Telegram never delivers a forward to it. "
         + "Replying is the mechanism, and attachments travel with a reply." }]],
  ], { shade: WARN_BG }),

  // ------------------------------------------------------------------
  H1("13.  Not built yet"),
  table(TWO, ["Piece", "Position"], [
    [[{ t: "Two-sided tickets", b: true }],
     [{ t: "One request living in a client's group and a supplier's group at "
         + "once, each seeing only their half. Specified, not started. Today a "
         + "request has one counterparty group, which is what makes sending to "
         + "the wrong party impossible by construction." }]],
    [[{ t: "The Finance FX flow", b: true }],
     [{ t: "Rate request, supplier quote, client acceptance, amount, "
         + "settlement, hash and summary. Specified in a separate note. Sits "
         + "behind two-sided tickets and cannot start before them." }]],
  ]),
  P("", { after: 60 }),
  P("Everything else in this document is live and in use.",
    { after: 0, italics: true, color: GREY }),
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
