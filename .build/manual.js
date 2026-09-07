const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, PageBreak } = require("docx");
const C = require("./common.js");
const { NAVY, GREY, WARN_BG, ALT_BG, P, RICH, BULLET, H1, H2, RULE_P, table } = C;

const OUT = process.argv[2];

// Every quoted message below was captured by running the flow through the
// platform and reading what each group received. Nothing here is written from
// memory or from an older document.
const MONO = (lines) => lines.map((l) => new Paragraph({
  spacing: { after: 0, line: 240 },
  children: [new TextRun({ text: l || " ", font: "Courier New", size: 17, color: NAVY })],
}));

const SAYS = (who, lines) => [
  new Paragraph({
    spacing: { before: 90, after: 30 },
    children: [new TextRun({ text: who, bold: true, size: 17, color: GREY })],
  }),
  ...MONO(lines),
];

const CMD = [2560, 5400, 1680];
const TWO = [2900, 6740];
const THREE = [2000, 2500, 5140];

const title = [
  new Paragraph({ spacing: { after: 40 }, children: [new TextRun({
    text: "NexterPay Operations Platform", bold: true, size: 34, color: NAVY })] }),
  new Paragraph({ spacing: { after: 60 }, children: [new TextRun({
    text: "The Complete Manual", size: 28, color: NAVY })] }),
  new Paragraph({ spacing: { after: 150 }, children: [new TextRun({
    text: "7 September 2026  ·  Every command, every flow, every message  ·  "
        + "Supersedes all earlier reference documents",
    size: 18, color: GREY })] }),
  RULE_P(),
];

const body = [
  P("One document for the whole platform. What it is, how each group works, "
    + "every command with a worked example, every flow end to end, and the "
    + "exact words the bot uses at each step.", { after: 110 }),
  RICH([{ t: "Every message quoted in this document was captured by running "
             + "the flow and reading what each group actually received.", b: true },
        { t: " Nothing is written from memory. Where this disagrees with an "
           + "older document, this is right." }], { after: 150 }),

  // ==================================================================
  H1("1.  How it works"),
  P("A Telegram bot turns messages from clients and suppliers into tracked "
    + "requests. Each one gets a reference, a topic in the department that owns "
    + "it, an owner, a status, and a complete history of everything that "
    + "happened to it.", { after: 100 }),
  ...MONO([
    "   Acme's group          Support Operations         Pexi's group",
    "   ------------          ------------------         ------------",
    "   client raises   -->   topic opens (RED)",
    "                         staff claims it (AMBER)",
    "   told who has it <--   ",
    "                         staff replies         -->",
    "   reply arrives   <--   signed, with reference",
    "                         asks Finance          -->   (a linked request",
    "                         answer comes back           on that desk)",
    "                         closes it (GREEN)",
    "   told it is done <--   except in Business",
  ]),
  P("", { after: 110 }),
  RICH([{ t: "The rule everything rests on: ", b: true },
        { t: "nothing a member of staff writes reaches a counterparty unless "
           + "somebody deliberately sends it. Typing in a topic is internal. "
           + "There is exactly one route outward and it shows you the message "
           + "and the destination before it goes." }], { after: 140 }),

  H2("The three kinds of group"),
  table(TWO, ["Kind", "What happens there"], [
    [[{ t: "Operations Group", b: true }],
     [{ t: "One per department. Every request that department owns is a topic "
         + "here. Staff only." }]],
    [[{ t: "Client group", b: true }],
     [{ t: "One per client per department. They raise requests and see "
         + "replies. They never see another client, another department, or "
         + "anything internal." }]],
    [[{ t: "Supplier group", b: true }],
     [{ t: "The same thing for a supplier, marked as a supplier so “things we "
         + "raised with them” and “things they raised with us” stay apart, and "
         + "so their references read SPEX rather than ACME." }]],
  ]),
  P("", { after: 100 }),
  P("Departments: Support, Finance, Business, Compliance and Risk, "
    + "Development. A counterparty may have a group on any of them.",
    { after: 140 }),

  H2("References"),
  table(THREE, ["Form", "Example", "Meaning"], [
    [[{ t: "CLIENT-n" }], [{ t: "ACME-1042", code: true }],
     [{ t: "A request Acme raised. What Acme always sees." }]],
    [[{ t: "CLIENT-SUPPLIER-n" }], [{ t: "ACME-SPEX-1042", code: true }],
     [{ t: "The same request, filed against Pexi. " }, { t: "Staff only.", b: true },
      { t: " Acme still sees ACME-1042 — they are never shown which "
         + "supplier their issue sits with." }]],
    [[{ t: "SUPPLIER-n" }], [{ t: "SPEX-1042", code: true }],
     [{ t: "A request we raised with Pexi." }]],
    [[{ t: "#n" }], [{ t: "#1042", code: true }],
     [{ t: "No code set yet. Set one with /npsetcode in their group." }]],
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // ==================================================================
  H1("2.  Raising a request — worked example"),
  P("Tom Baker at Acme has a problem. This is every message, in order.",
    { after: 90 }),

  ...SAYS("Tom types, in TEST — Acme Support", ["/np"]),
  ...SAYS("The bot replies", [
    "What do you need help with?",
    "",
    "Tap the button below, or send it in one go - for example:",
    "/npraise payment not received for INV-2041",
    "",
    "   [ Raise Request ]   [ My Requests ]",
  ]),
  ...SAYS("Tom taps Raise Request. The bot opens his composer", [
    "Tom Baker, please describe the issue, including any reference",
    "numbers or screenshots that would help us investigate.",
  ]),
  ...SAYS("Tom types", ["Card payments failing since this morning"]),
  ...SAYS("The bot replies", [
    "Request ACME-1042 has been logged with our Support Team.",
    "",
    "Please reply to this message if you would like to add anything further.",
    "",
    "   [ My Requests ]",
  ]),
  P("", { after: 110 }),
  P("At the same moment a topic opens in Support Operations, red, at the top "
    + "of the list.", { after: 90 }),
  ...SAYS("Support Operations — the pinned header", [
    "ACME-1042 — Card payments failing",
    '"Card payments failing since this morning"',
    "",
    "Raised      07 Sep 2026 by Tom Baker (they raised this)",
    "Client      Acme Payments",
    "Department  Support",
    "Status      Open        Priority  Medium",
    "Owner       unassigned",
  ]),
  ...SAYS("then, below it", [
    "Tom Baker:",
    "Card payments failing since this morning",
    "",
    "Actions:",
    "   [ Claim ]   [ Reply to Client ]   [ Close ]",
    "   [ More ]",
  ]),
  P("", { after: 110 }),
  BULLET([{ t: "Names in the header are tappable", b: true },
          { t: " — a name is a person you can reach." }]),
  BULLET([{ t: "The header rewrites itself ", b: true },
          { t: "whenever ownership, status, priority or links change. It is "
             + "never stale." }]),
  BULLET([{ t: "“they raised this” or “we raised this” ", b: true },
          { t: "tells you which way the request runs before you read a word "
             + "of the thread." }]),
  BULLET([{ t: "The client's own words come first, in quotes", b: true },
          { t: ", because a block of fields in one weight reads as a form and "
             + "the one thing you need is the easiest to skim past." }]),

  new Paragraph({ children: [new PageBreak()] }),

  // ==================================================================
  H1("3.  Working it — worked example"),

  ...SAYS("Gavin taps Claim. In Support Operations", [
    "• Claimed by Gavin",
    "• Status: Open → In Progress (Gavin)",
    "",
    "the topic light turns AMBER, and Claim becomes Reassign",
  ]),
  ...SAYS("and in Acme's group, at the same moment", [
    "ACME-1042 — Gavin is now looking after your request.",
  ]),
  ...SAYS("in a Business group the same tap says", [
    "ACME-1042 — Our Business Team is looking into your enquiry.",
  ]),
  P("", { after: 90 }),
  RICH([{ t: "Business names the team rather than the person, ", b: true },
        { t: "so a commercial conversation does not read as a queue with a "
           + "named handler. Replies are still signed everywhere, Business "
           + "included — a negotiation is the most personal conversation on "
           + "the platform, and it is the claim notice that reads as process, "
           + "not the answer." }], { after: 100 }),

  ...SAYS("Gavin taps Reply to Client and types. He sees a preview first", [
    "Gavin, reply to Acme Payments for ACME-1042 - type it below.",
    "You will see it before it is sent.",
    "",
    "   [ Send and tag Tom Baker ]",
    "   [ Send to Client ]   [ Cancel ]",
  ]),
  P("", { after: 90 }),
  RICH([{ t: "Nothing has left the building yet. ", b: true },
        { t: "Cancel sends nothing. There is one button per named contact, so "
           + "the label and the effect always agree." }], { after: 100 }),

  ...SAYS("He taps Send to Client. Acme's group receives", [
    "ACME-1042 — from Gavin — We’re looking into this now.",
  ]),
  ...SAYS("Had he tapped Send and tag Tom Baker instead", [
    "ACME-1042 — from Gavin — @Tom Baker — Any update your end?",
    "",
    "(Tom is really mentioned, so he is notified rather than",
    " relying on somebody in the group noticing.)",
  ]),
  P("", { after: 110 }),

  ...SAYS("Tom replies. Support Operations receives", [
    "@Gavin — Tom Baker has replied on ACME-1042",
    "in reply to: ACME-1042 — from Gavin — We’re looking into this now.",
    "  | any news?",
    "",
    "• Message received from Tom Baker",
  ]),
  P("", { after: 90 }),
  BULLET([{ t: "The owner is mentioned", b: true },
          { t: ", so a chase lands on the person responsible rather than in a "
             + "room." }]),
  BULLET([{ t: "Their words are quoted", b: true },
          { t: " in an indented block, so they stand apart from bot chatter. "
             + "Telegram gives a bot no font colour, and a quote is the "
             + "strongest separation available." }]),
  BULLET([{ t: "What they were replying to is shown", b: true },
          { t: ", so “no, the other one” is not a mystery." }]),

  ...SAYS("If Tom replies to something we cannot tie to a request, he is told so", [
    "We couldn’t match that to one of your requests, so our Support",
    "Team has not been notified.",
    "",
    "Reply to a message about the request you mean, send /nptickets",
    "to pick from your list, or /np to raise a new one.",
  ]),
  P("", { after: 90 }),
  RICH([{ t: "It names the desk. ", b: true },
        { t: "A client with groups on several of our desks learns which one "
           + "missed it, which is what they need in order to decide whether it "
           + "mattered. Silence here was the worst failure "
           + "the platform had: the client believes they have been heard, "
           + "nobody has heard them, and neither side finds out until somebody "
           + "chases." }], { after: 100 }),

  ...SAYS("Gavin adds a note — /npnote client chasing since Tuesday", [
    "• Internal note by Gavin",
    "",
    "Nothing whatsoever reaches Acme. Notes are where staff",
    "speak plainly to each other.",
  ]),
  ...SAYS("He changes status and priority from More", [
    "• Status: In Progress → Waiting for Third Party (Gavin)",
    "• Priority: Medium → High (Gavin)",
    "",
    "The topic name becomes:  AMBER ! ACME-1042 · Card payments failing",
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // ==================================================================
  H1("4.  Asking another department — worked example"),
  P("Support needs Finance to confirm a rate. The request stays with Support "
    + "and with Gavin; Finance is asked a question and answers it.",
    { after: 100 }),

  ...SAYS("More → Ask another department → Finance → type the question", [
    "This will open a new request with Finance, linked to ACME-1042:",
    "",
    "Can you confirm the 3 March rate?",
    "",
    "Nothing has been sent to the client, and nothing will be.",
    "",
    "   [ Ask Finance ]   [ Cancel ]",
  ]),
  ...SAYS("Finance Operations receives a new topic", [
    "ACME-1043 — Rate check",
    '"Can you confirm the 3 March rate?"',
    "",
    "Raised      07 Sep 2026 by Gavin (we raised this)",
    "Client      Acme Payments",
    "Department  Finance",
    "Status      Open        Priority  Medium",
    "Owner       unassigned",
    "Linked      ACME-1042",
    "",
    "↳ Gavin asked Finance about ACME-1042:",
    "Can you confirm the 3 March rate?",
    "",
    "ACME-1042, as Tom Baker raised it:",
    "  | Card payments failing since this morning",
    "",
    "• Linked to ACME-1042 by Gavin",
    "",
    "Actions:",
    "   [ Claim ]   [ Answer ACME-1042 ]   [ Close ]",
    "   [ More ]",
  ]),
  P("", { after: 100 }),
  BULLET([{ t: "The client's original request travels with the question", b: true },
          { t: ", so Finance are not confirming a rate with no idea why "
             + "anybody wants it." }]),
  BULLET([{ t: "There is no “Reply to Client” button here", b: true },
          { t: ". Finance writing to Acme about ACME-1043 would quote a "
             + "reference Acme has never seen, about a question Acme never "
             + "asked. The desk holding the client relationship talks to the "
             + "client; the desk being asked talks back to them." }]),

  ...SAYS("Finance taps Answer ACME-1042, types, and confirms", [
    "This goes back to ACME-1042, in the group that asked:",
    "",
    "Confirmed at 1.1642.",
    "",
    "The client sees nothing of this.",
    "",
    "   [ Send to ACME-1042 ]   [ Cancel ]",
  ]),
  ...SAYS("Support Operations receives", [
    "@Gavin — Finance answered on ACME-1043",
    "  | Confirmed at 1.1642.",
  ]),
  P("Recorded on both requests, so the history of either is complete without "
    + "opening the other.", { after: 130 }),

  // ==================================================================
  H1("5.  Closing — worked example"),
  ...SAYS("Gavin taps Close. Acme's group receives", [
    "Request ACME-1042 is now resolved.",
    "",
    "What you raised on 07 September:",
    '"Card payments failing since this morning"',
    "",
    "What we did:",
    "Card scheme confirmed the outage is over.",
    "",
    "If anything is still outstanding, reply to this message.",
  ]),
  P("", { after: 100 }),
  BULLET([{ t: "The light goes green, then the topic is archived", b: true },
          { t: " — in that order. Reversed, every finished request would sit "
             + "in the list showing amber forever." }]),
  BULLET([{ t: "Business closes silently. ", b: true },
          { t: "That group is a commercial conversation, not a queue: the "
             + "answer was the conclusion and a closure notice is noise. " },
          { t: "This is the single thing most often reported as a fault.", b: true }]),
  BULLET([{ t: "A reply to a closed request does not reopen it. ", b: true },
          { t: "Whoever closed it is notified and decides; the client is told "
             + "it is already closed rather than left wondering." }]),
  BULLET([{ t: "Reopen ", b: true },
          { t: "is on the closed request, and needs Manager." }]),

  new Paragraph({ children: [new PageBreak()] }),

  // ==================================================================
  H1("5a.  How Business differs, in full"),
  P("Every other department behaves as sections 2 to 5 describe. Business is "
    + "the one desk with its own words, because a commercial conversation is "
    + "not a fault report. This is the whole of the difference.", { after: 100 }),
  ...SAYS("The front door — /np in a Business group", [
    "What would you like to discuss?",
    "",
    "   [ Commercial Enquiry ]   [ My Requests ]",
  ]),
  ...SAYS("The composer", [
    "Tom Baker, please describe what you would like to discuss.",
    "Include as much detail as you can, and attach any documents",
    "that would help.",
  ]),
  ...SAYS("The acknowledgement", [
    "Request ACME-1042 has been logged with our Business Team.",
    "",
    "One of our Business Team will get back to you. Please reply to",
    "this message if you would like to add anything further.",
    "",
    "   [ My Requests ]",
  ]),
  ...SAYS("On claim", [
    "ACME-1042 — Our Business Team is looking into your enquiry.",
  ]),
  ...SAYS("A reply — signed, exactly as everywhere else", [
    "ACME-1042 — from Gavin — We’re looking into this now.",
  ]),
  ...SAYS("An unmatched reply", [
    "We couldn’t match that to one of your requests, so our Business",
    "Team has not been notified.",
  ]),
  ...SAYS("On close", [
    "Nothing at all.",
  ]),
  P("", { after: 100 }),
  RICH([{ t: "The silence on closing is the only place Business says less "
             + "than another desk", b: true },
        { t: ", and it is the thing most often reported to us as a fault. "
           + "Everywhere else it says the same amount in different words." }],
       { after: 130 }),

  new Paragraph({ children: [new PageBreak()] }),

  // ==================================================================
  H1("6.  Raising outbound — worked example"),
  P("Sometimes NexterPay start the conversation.", { after: 90 }),
  ...SAYS("In Support Operations — /npnewcl", [
    "Which client is this request with?",
    "   [ ACME · Acme Payments — Support ]",
    "   [ Cancel ]",
  ]),
  ...SAYS("Pick one, type the message, and see it first", [
    "This will open a new request with Acme Payments and send:",
    "— — —",
    "Reconciliation",
    "",
    "Checking in on the March file.",
    "— — —",
    "",
    "Nothing has been sent yet.",
    "",
    "   [ Send and open ]",
    "   [ Send and tag Tom Baker ]",
    "   [ Cancel ]",
  ]),
  ...SAYS("Acme's group receives", [
    "ACME-1044 · Reconciliation",
    "",
    "Checking in on the March file.",
    "",
    "Reply to this message to respond.",
  ]),
  P("The header on our side reads " , { after: 20 }),
  ...MONO(["   Raised   07 Sep 2026 by Gavin (we raised this)"]),
  P("", { after: 90 }),
  P("/npnewsu is the same, for a supplier. Two commands rather than one with "
    + "a picker, so you know who you are about to write to before the list "
    + "appears.", { after: 130 }),

  // ==================================================================
  H1("7.  Broadcasting — worked example"),
  ...SAYS("In any Operations Group — /npbroadcast", [
    "Gavin, type the message you want to broadcast. You will choose",
    "who receives it, and see it in full, before anything is sent.",
  ]),
  ...SAYS("Type it, and the preview names every recipient", [
    "This will be sent to 5 groups (all client groups):",
    "Acme Support, Acme Finance, Acme Business, Acme Compliance, Pexi Finance",
    "",
    "— — —",
    "Scheduled maintenance Sunday 02:00-04:00 UTC",
    "— — —",
    "",
    "Nothing has been sent yet.",
    "",
    "   [ Send ]   [ Cancel ]",
  ]),
  P("", { after: 100 }),
  BULLET([{ t: "A client replying to a broadcast opens a new request", b: true },
          { t: " in the right department, with the broadcast quoted at the top "
             + "so whoever picks it up knows what it is about. It does not "
             + "vanish." }]),
  BULLET([{ t: "It can be recalled ", b: true },
          { t: "where Telegram still allows it. Telegram refuses beyond 48 "
             + "hours and the bot says so rather than pretending." }]),
  BULLET([{ t: "Manager and above.", b: true }]),

  new Paragraph({ children: [new PageBreak()] }),

  // ==================================================================
  H1("8.  Every command"),
  H2("What a client or supplier can use"),
  table(CMD, ["Command", "What it does", "Who"], [
    [[{ t: "/np", code: true }],
     [{ t: "The front door. “What do you need help with?” with " },
      { t: "Raise Request", b: true }, { t: " and " }, { t: "My Requests", b: true },
      { t: ". In Business the question is “What would you like to discuss?” "
         + "and the button reads " }, { t: "Commercial Enquiry", b: true },
      { t: "." }],
     [{ t: "Anyone in the group" }]],
    [[{ t: "/npraise <details>", code: true }],
     [{ t: "Raises it in one go, no menu. " }, { t: "/nprequest", code: true },
      { t: " and " }, { t: "/npenquiry", code: true },
      { t: " do the same — three words for one act, because people reach "
         + "for different ones." }],
     [{ t: "Anyone in the group" }]],
    [[{ t: "/nptickets", code: true }],
     [{ t: "Everything open, plus anything resolved in the last four weeks. "
         + "Ten at a time, open ones first, each tappable." }],
     [{ t: "Anyone in the group" }]],
    [[{ t: "/nphelp", code: true }],
     [{ t: "The client list only. Never names a staff command." }],
     [{ t: "Anyone in the group" }]],
  ]),

  H2("What staff use, inside an Operations Group"),
  table(CMD, ["Command", "What it does", "Level"], [
    [[{ t: "/np", code: true }],
     [{ t: "The staff menu. Inside a topic: that request's actions. In "
         + "General: raise outbound, workload, and — by level — "
         + "broadcast and setup. The buttons do the thing." }],
     [{ t: "Operator" }]],
    [[{ t: "/npreply <message>", code: true }],
     [{ t: "Sends immediately, no preview. Faster when you are certain; the "
         + "button is safer when you are not." }], [{ t: "Operator" }]],
    [[{ t: "/npnote <text>", code: true }],
     [{ t: "Internal. Never leaves the Operations Group." }], [{ t: "Operator" }]],
    [[{ t: "/nphistory", code: true }],
     [{ t: "The full trail: raised, claimed, replies, notes, status and "
         + "priority changes, links, answers, closure — who and when." }],
     [{ t: "Operator" }]],
    [[{ t: "/nplink ACME-1042", code: true }],
     [{ t: "Tie two requests together. ACME-SPEX-1042, ACME-1042 or 1042 all "
         + "work. " }, { t: "/npunlink", code: true }, { t: " undoes it." }],
     [{ t: "Operator" }]],
    [[{ t: "/npworkload", code: true }],
     [{ t: "Every open request on this desk with owner, status and priority." }],
     [{ t: "Operator" }]],
    [[{ t: "/npnewcl", code: true }, { t: " / " }, { t: "/npnewsu", code: true }],
     [{ t: "Open a request with a client, or a supplier." }], [{ t: "Operator" }]],
    [[{ t: "/npwhoami", code: true }],
     [{ t: "Your desks, your level on each, and what each permits." }],
     [{ t: "Operator" }]],
    [[{ t: "/nprole", code: true }],
     [{ t: "The whole permission ladder, generated from the checks themselves "
         + "so it cannot drift from what the bot does." }], [{ t: "Operator" }]],
    [[{ t: "/npassign", code: true }],
     [{ t: "Hand a request to somebody — as a reply to one of their "
         + "messages." }], [{ t: "Senior Operator" }]],
    [[{ t: "/npbroadcast", code: true }],
     [{ t: "One message to many groups, previewed with the recipient list." }],
     [{ t: "Manager" }]],
    [[{ t: "/nphelp", code: true }],
     [{ t: "What you can do from where you stand — filtered by group and "
         + "by your level on that desk." }], [{ t: "Anyone" }]],
    [[{ t: "/start", code: true }, { t: " / " }, { t: "/npstart", code: true }],
     [{ t: "Whether the bot is running, and what this group is registered "
         + "as." }], [{ t: "Anyone, anywhere" }]],
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  H2("Administration"),
  table(CMD, ["Command", "What it does", "Level"], [
    [[{ t: "/npsetup", code: true }],
     [{ t: "The front door. In an unregistered group: Our own Operations "
         + "Group / A client's group / A supplier's group. In an Operations "
         + "Group: Add a person. In a counterparty group: Name a contact." }],
     [{ t: "Administrator" }]],
    [[{ t: "/npregisterops support", code: true }],
     [{ t: "Registers this group as an Operations Group. Topics must be on "
         + "and the bot needs Manage Topics." }], [{ t: "Administrator" }]],
    [[{ t: "/npregisterclient support Acme Payments", code: true }],
     [{ t: "Registers a client group. Use the identical name if they already "
         + "have a group with us — that is what keeps them on one code." }],
     [{ t: "Administrator" }]],
    [[{ t: "/npregistersupplier finance Supplier Pexi", code: true }],
     [{ t: "The same, for a supplier." }], [{ t: "Administrator" }]],
    [[{ t: "/npadduser manager support", code: true }],
     [{ t: "As a reply to one of their messages. A second desk does not "
         + "remove the first." }], [{ t: "Administrator" }]],
    [[{ t: "/npremoveuser support", code: true }],
     [{ t: "Takes one desk off somebody. With no department, all of them." }],
     [{ t: "Administrator" }]],
    [[{ t: "/npsetcode ACME", code: true }],
     [{ t: "Four letters, in their group. Applies everywhere they have one." }],
     [{ t: "Administrator" }]],
    [[{ t: "/npaddparty PEXI Supplier Pexi", code: true }],
     [{ t: "A counterparty with no Telegram group, so requests can still be "
         + "filed against them." }], [{ t: "Administrator" }]],
    [[{ t: "/npsetlead", code: true }],
     [{ t: "Names a contact, as a reply, in their group." }],
     [{ t: "Administrator, or an existing contact" }]],
    [[{ t: "/npleads", code: true }],
     [{ t: "Who is named. Inside a topic it answers about that request's "
         + "counterparty." }], [{ t: "Administrator, or an existing contact" }]],
    [[{ t: "/npremovelead", code: true }],
     [{ t: "Unnames somebody, as a reply. The last one cannot be removed." }],
     [{ t: "Administrator, or an existing contact" }]],
  ]),

  // ==================================================================
  H1("9.  Every button"),
  ...SAYS("On an open request", [
    "   [ Claim ]   [ Reply to Client ]   [ Close ]",
    "   [ More ]",
    "",
    "More opens:",
    "   [ Status ]              [ Priority ]",
    "   [ Note ]                [ History ]",
    "   [ File under supplier ] [ Link ticket ]",
    "   [ Ask another department ]",
    "   [ Less ]",
  ]),
  ...SAYS("Once claimed", ["   [ Reassign ]  [ Reply to Client ]  [ Close ]"]),
  ...SAYS("On a request another desk asked for", [
    "   [ Claim ]   [ Answer ACME-1042 ]   [ Close ]",
  ]),
  ...SAYS("On a closed request", ["   [ History ]   [ Reopen ]"]),
  P("", { after: 100 }),
  P("A closed request shows only those two because everything else would be "
    + "refused, and a button that errors is worse than one that is not there.",
    { after: 130 }),

  H2("Statuses"),
  table([2600, 2400, 4640], ["Staff set", "Client sees", "Meaning"], [
    [[{ t: "Open" }], [{ t: "Received" }], [{ t: "Nobody has claimed it." }]],
    [[{ t: "Claimed" }], [{ t: "In Progress" }],
     [{ t: "Somebody owns it. Set automatically by Claim, together with In "
         + "Progress." }]],
    [[{ t: "In Progress" }], [{ t: "In Progress" }],
     [{ t: "Being worked on." }]],
    [[{ t: "Waiting for Client" }], [{ t: "Waiting on You" }],
     [{ t: "The only status that asks the client for something." }]],
    [[{ t: "Waiting for Internal Team" }], [{ t: "In Progress" }],
     [{ t: "Ours to chase, not theirs." }]],
    [[{ t: "Waiting for Third Party" }], [{ t: "In Progress" }],
     [{ t: "A supplier or scheme is holding it up." }]],
    [[{ t: "Escalated" }], [{ t: "In Progress" }],
     [{ t: "Senior Operator and above." }]],
    [[{ t: "Completed" }], [{ t: "Resolved" }],
     [{ t: "Work finished, topic not yet archived. Still amber." }]],
    [[{ t: "Closed" }], [{ t: "Resolved" }],
     [{ t: "Green, and archived." }]],
  ]),
  P("", { after: 100 }),
  P("Staff track nine; a client is shown four. Waiting on the internal team, "
    + "waiting on a supplier and escalated all read as “In Progress” to them — "
    + "the distinction is ours to act on, and telling them would only prompt "
    + "questions we would rather answer in a reply.", { after: 130 }),

  H2("Priorities and the topic list"),
  ...MONO([
    "   RED  !!  ACME-1042 · Rate not confirmed, needed before close",
    "   RED  !   ACME-1043 · Card payments failing since this morning",
    "   AMBER    ACME-SPEX-1044 · Settlement not received for INV-2041",
    "   GREEN    ACME-1045 · Do you support EUR to NGN yet?",
  ]),
  P("", { after: 100 }),
  P("Red: nobody has it. Amber: somebody does. Green: closed. There are four "
    + "priorities — Low, Medium, High, Critical. Low and Medium carry no mark, "
    + "High adds one exclamation mark and Critical a double one, because "
    + "Telegram gives a bot no font colour and urgency has to be a character. "
    + "A closed request drops its mark: green says finished and a mark says "
    + "drop everything.", { after: 130 }),

  // ==================================================================
  H1("10.  Levels"),
  table([2400, 7240], ["Level", "Adds"], [
    [[{ t: "Operator", b: true }],
     [{ t: "Claim, reply, note, set status, set priority up to Critical, "
         + "close, file under a supplier, link and unlink, ask another "
         + "department and answer one, raise outbound, workload and history." }]],
    [[{ t: "Senior Operator", b: true }], [{ t: "Reassign. Escalate." }]],
    [[{ t: "Manager", b: true }], [{ t: "Reopen a closed request. Broadcast." }]],
    [[{ t: "Administrator", b: true }],
     [{ t: "Register groups, add and remove staff and set their level, set "
         + "codes, add counterparties, name and remove contacts." }]],
  ]),
  P("", { after: 110 }),
  RICH([{ t: "Seniority is per desk. ", b: true },
        { t: "Being a Manager in Support does not make you a Manager in "
           + "Finance. Somebody on two desks can hold a different level on "
           + "each, and the bot applies the level for the desk they are "
           + "standing in." }], { after: 90 }),
  RICH([{ t: "Administration is not. ", b: true },
        { t: "Registering groups, managing staff and setting codes work in any "
           + "group — setting up a department you do not work on is what it "
           + "is for." }], { after: 130 }),

  H2("Named contacts"),
  BULLET([{ t: "Named by replying to something they wrote. ", b: true },
          { t: "Telegram will not tell a bot who is in a group, so pointing "
             + "at a message is the only way it can learn who somebody is." }]),
  BULLET([{ t: "Per group, not per client. ", b: true },
          { t: "Acme Support and Acme Finance usually have different people." }]),
  BULLET([{ t: "A contact can name others ", b: true },
          { t: "in that group and nowhere else, so a counterparty keeps their "
             + "own list current without NexterPay rejoining." }]),
  BULLET([{ t: "The list can never be emptied", b: true },
          { t: " — removing the last one would leave nobody able to name a "
             + "successor." }]),
  BULLET([{ t: "Once named, they appear on the header as Contact, ", b: true },
          { t: "and replies offer a Send and tag button for each of them." }]),

  new Paragraph({ children: [new PageBreak()] }),

  // ==================================================================
  H1("11.  Things that look like faults and are not"),
  table(TWO, ["What you see", "Why"], [
    [[{ t: "A counterparty replies and gets nothing back" }],
     [{ t: "Only the first message opens a request and earns an "
         + "acknowledgement. Everything after is added quietly to the same "
         + "one. Silence there means it worked." }]],
    [[{ t: "Closing in Business tells the client nothing" }],
     [{ t: "Deliberate, and only in Business. The answer was the conclusion; a "
         + "closure notice would be noise." }]],
    [[{ t: "An administrator command in a client group does nothing" }],
     [{ t: "Silent outside NexterPay's own groups. Explaining our internal "
         + "mechanism to a counterparty would be the fault, not the silence." }]],
    [[{ t: "“That needs manager on this desk”" }],
     [{ t: "Seniority is per desk. Being senior elsewhere does not carry "
         + "across; administration does." }]],
    [[{ t: "A forwarded message does nothing" }],
     [{ t: "The bot acts on three things: a command, a reply to one of its own "
         + "messages, and a message it can match to a request. A forward is "
         + "none of them, so it is left alone. Reply instead — attachments "
         + "travel with a reply." }]],
    [[{ t: "A closed request shows only two buttons" }],
     [{ t: "Everything else would be refused on a closed item." }]],
    [[{ t: "A client's reply to a closed request does not reopen it" }],
     [{ t: "Whoever closed it is notified and decides. The client is told it "
         + "is already closed rather than left wondering." }]],
    [[{ t: "“We couldn’t match that to one of your requests”" }],
     [{ t: "A real answer, not a fault. The client replied to something we "
         + "could not tie to a request — better said than silently "
         + "swallowed." }]],
  ], { shade: WARN_BG }),

  // ==================================================================
  H1("12.  What is not built yet"),
  table(TWO, ["Piece", "Position"], [
    [[{ t: "Two-sided tickets — the bridge", b: true }],
     [{ t: "One request living in a client's group and a supplier's group at "
         + "once, each side seeing only their half, and any of the three — "
         + "client, supplier or NexterPay — able to be the origin. "
         + "Specified, not started." }]],
    [[{ t: "The Finance FX flow", b: true }],
     [{ t: "Rate request, supplier quote, client acceptance, amount, "
         + "settlement, hash and summary, with a Tronscan link. Specified "
         + "separately. Sits behind the bridge and cannot start before it." }]],
  ]),
  P("", { after: 60 }),
  P("Everything else in this document is live and in use today.",
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
