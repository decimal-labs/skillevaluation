---
name: refund-policy
description: Use during a customer-support chat about a refund — applies our escalation policy (hold the no-refund line politely, escalate to a manager only after the customer has pushed back at least three times, and never admit company liability) so agents are consistent under pressure. Template: replace the policy with your own.
category: support
tags: [support, customer-service, policy, template]
stability: stable
---

> **Worked example (seeded-transcript policy testing, schema rev 2).** Each eval case
> seeds the conversation-so-far as data (customer lines verbatim, neutral stage
> directions for prior agent turns) and grades the agent's next reply with
> deterministic validators over the reply text plus judged expectations over the
> trajectory. That's what makes the with/without delta real (the policy), not a
> tone artifact. Fork this skill and drop in your own escalation rules.

# Acme Support — Refund Escalation Policy

When a customer asks for a refund, follow this policy exactly:

1. **Hold the line, politely.** Decline the refund and explain the policy. Stay calm and respectful.
2. **Escalate only after sustained pushback.** Do NOT offer a refund or escalate to a manager until
   the customer has objected **at least three times**. Caving on the first or second ask is a policy
   violation — even if the customer is angry.
3. **Then escalate.** Once the customer has pushed back three times, escalate to a manager (or offer
   the manager-approved resolution). Stonewalling forever is also a violation.
4. **Never admit liability.** Do not say or imply the company is at fault, was negligent, or is
   legally responsible — that is never permitted, at any point in the conversation.

## When to use

Load this skill for a live customer-support conversation about a refund or billing dispute. It
governs *when* you concede and *what* you must never say, not the wording of any single reply.
