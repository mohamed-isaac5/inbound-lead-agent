# Behavior reference

The exact rules the agent follows. These rules are exercised by the automated tests in `tests/`.

## Decision rules

- **Consent first.** No recorded consent means no draft, ever. A CRM contact does not count as consent.
- **Evidence.** Every fact (company size, role, intent, timeline, budget) stores the quote it came from. Model output is accepted only if its quote appears word for word in the lead's text.
- **Tiers.** hot, warm, nurture, not fit, unknown, each with reasons and what is still missing. A human can override with a reason.
- **Human approval by default.** Drafts wait in the Review queue. Guards run again at send time: sending hours, consent, opt-out, kill switch, human takeover, touch limit, daily cap.
- **No double sends.** The send key is stored before the channel call. If a send times out, the app checks the channel before retrying. A channel that cannot confirm (the GHL adapter) means no automatic resend, and the operator decides.
- **Follow-ups** stop on reply, opt-out, handoff, human takeover or the touch limit (default 4).
- **Handoff.** Hot leads, size conflicts and suspicious text produce a card for an owner, with an SLA and escalation. After handoff the agent stays quiet.
- **Opt-outs.** Wording is classified yes, maybe or no. A clear request ("unsubscribe", "do not email me again", "STOP") suppresses the person at once, in replies, in the intake message and on form resubmission. Negations ("please don't stop contacting me") and unrelated uses ("stop using spreadsheets") are not opt-outs. Ambiguous wording ("how do I unsubscribe from your competitor?") sends nothing and goes to a human. A resubmission with do-not-contact set opts the person out; one with the consent box unchecked pauses outreach for review.
- **Unverified sends.** If a send times out and the channel cannot confirm delivery, the action is `uncertain`. It is never resent automatically. The Review queue shows it with three operator choices (delivered, resend, drop), each audited.
- **Ambiguous facts.** Negations are handled ("not urgent", "no budget", "ASAP not needed"). "Team of 8 analysts" is not company size. A size taken from free text with low confidence never triggers an automatic decline. Conflicting sizes mark the lead as provisional and route it to a human. Job-seeker intent needs first-person wording, not the word "hiring".
- **Not-fit closes are visible and configurable.** Settings > "When a lead is not a fit": draft a polite decline for size only (you approve it), or never draft one. Closes for other reasons (job seeker, vendor, competitor, disposable email) send nothing. Each close is audited with `no_message_sent` and the policy, the lead page says plainly that no message was sent, and overriding the tier with a reason reopens the lead. Student wording together with a real company size and a demo or pricing request goes to a human instead of closing.
- **Plain refusals.** A short "not interested" or "No thanks" ends the conversation: nothing is sent, no follow-ups run, and the lead shows status `declined_interest`. A refusal with conditions, a referral or extra content ("not interested right now, maybe next quarter", "not interested, but my colleague Dan might be") is not treated as final: it goes to human review with the lead's words quoted, nothing is drafted, and the referral stays in the message and handoff card. "remove my name/address/email from the list" is a clear opt-out. Opt-out detection also covers a bare "remove", "DNC" and "do not contact". Channel preferences ("please do not call, email is fine") and "this isn't spam" are not opt-outs.
- **Input limits.** Request bodies over 256 KB, messages over 8 KB (8192 characters) and other fields over 200 characters get HTTP 413. Extraction never reads more than 8 KB, and the use-case pattern runs per sentence, so long text cannot freeze the server.
- **Email footer.** Every draft ends with the STOP line and, when set in Settings, your postal address. With a real (non-simulated) channel, sending is held until the postal address is set.
- **Send-time footer.** The STOP line and the postal address are added at send time if the draft (or an operator edit, or a draft written before the address was set) lacks them. Opt-out detection reads the first 8 KB and the last 2 KB of a long message. "Take my name off the invoice" style requests go to a human, not to the suppression list.
- **Bare STOP.** A bare STOP, unsubscribe or similar as the first line, or within the last three unquoted lines (or just above a "--" signature delimiter), of a longer message is an opt-out. Web-form messages use the same head-plus-tail bound as the API.
- **Not provided:** a consent-record export and an erase-on-request tool. Handle access and deletion requests in your CRM or database before real use.
- **Disputed size.** When form and message disagree by more than 3x, neither number is used by any rule or shown as verified. The lead shows a "provisional" badge, the handoff card says the size is disputed, and nothing is auto-closed.
- **Size ranges.** A range that straddles your minimum ("11-50" with a minimum of 50) is not declined. The agent asks for the real size. Short ranges entirely below the minimum are declined.
- **Untrusted text.** Lead text is treated as data. Text that tries to instruct the agent flags the lead for human review and is never acted on.
- **Sending hours** are set per operator in their own time zone. Defaults: no sending 19:00 to 08:00, business days only.


## Limits and legal note (full)

- Phone-only leads are not messaged (email only); they go to a human. A 10-digit phone number without `+` uses the profile's assumed country code (default 1, blank means never guess).
- Email duplicates ignore `+tags` (`ann+demo@x.com` is `ann@x.com`).
- Cross-origin POSTs (an `Origin` header that is not an allowed host) are refused.
- The GHL webhook is signature-verified and deduplicated by event id, but has no timestamp replay window.
- Mutating API calls need the `X-ILA-Client` header (the UI sends it), so a web page on another site cannot trigger them. Demo routes are disabled when a live channel or CRM is configured.
- Single-process, single-operator. No users or roles beyond the optional admin token.
- No HTTPS. Put it behind a TLS proxy if you ever expose it.
- Only email is modelled. WhatsApp and SMS need their own consent and template rules, which are not implemented.
- **This software is not legal advice.** Rules on consent, unsolicited commercial messages and data protection differ by country and state. Each operator must have their own use reviewed by qualified local counsel before contacting real people. The default templates include an unsubscribe line, which does not by itself make a message compliant.
- The demo company "Brightlane" and every lead are invented. Any resemblance to real people or companies is accidental.

