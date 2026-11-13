# McNeese Partnership — Anticipated Questions & Speakable Answers

*Companion to `MCNEESE_RESEARCH_PARTNERSHIP_BRIEF.md`. Questions a research
partner / faculty member is likely to ask, with answers you can say out loud.*
*⚠︎ = harder / more skeptical. 🔴 = you must fill in a real specific before the meeting.*

---

## The research

**Q: What is "metacognitive AI" in plain terms?**
AI that reasons about its own thinking — it monitors what it knows, notices gaps,
reflects on its past behavior, and steers itself. Most AI just maps input to
output. Our research is about the *architecture around* the model that gives it
drive, memory, self-assessment, and trust.

**Q: What's genuinely novel here — what's the contribution?** ⚠︎
Five mechanisms, each addressing an open problem: (1) autonomous drive from an
"empty substrate as pressure" model — motivation as architecture; (2) a cognition
cycle with no central orchestrator; (3) a system that sets and revises its own
long-horizon direction; (4) a deterministic memory where every recalled fact
carries a cryptographic provenance chain and human input is gated as untrusted;
(5) periodic machine self-reflection over its own history. The field is almost
entirely focused on *scaling models*; we study *architecture*. That's the gap.

**Q: Isn't this just prompt-engineering on top of an existing LLM?** ⚠︎
No. The language model is one swappable component — deliberately *small* (~4B
parameters). The research is everything around it: how it drives itself, what it
remembers and how that memory is verified, how it reflects and re-steers. Swap the
model out and the architecture — the actual contribution — is unchanged. The point
is that the intelligence lives in the *structure*, which is why it runs on a
consumer machine instead of a data center.

**Q: Where are the publications / where's the peer review?** ⚠︎
We built the working system first — that's our evidence the mechanisms are real,
not conjecture. Peer-reviewed publication is one of the things this partnership is
*for*: academic rigor, co-authorship, and the credibility that a university
collaborator brings. We're bringing a working platform and open research
questions; McNeese brings the scholarly apparatus.

## The proof

**Q: Can you show it working?**
Yes. We can boot a cold system that, from nothing but its founding principles,
autonomously starts a reasoning episode and consolidates what it learns — the
drive mechanism operating live — offline, on one machine.

**Q: How good is it, honestly?**
On a standard long-term-memory benchmark it scores in the high-60s percent running
a single 4B model on one consumer iGPU. Our error analysis shows the limit is the
small answer-model, *not* the memory architecture — which is the encouraging part:
the architecture already does its job. We don't claim state-of-the-art accuracy; we
claim a novel architecture that earns strong results from design, on cheap hardware.

**Q: What are the honest limitations?** ⚠︎
It's a small-model system — raw fluency and breadth trail frontier cloud models.
It's early-stage research, not a productized tool. Larger-scale validation needs
more compute than a single consumer machine — which is part of what the partnership
provides. We're candid about all of this; overclaiming would be the fastest way to
lose an academic's trust.

## SBIR / STTR & funding

**Q: How does the funding actually work?**
Federal agencies run small-business research programs. SBIR funds R&D done mainly
by the small business. STTR funds the same kind of research but **requires** a
nonprofit research-institution partner — a university — that performs a defined
share of the work and is funded for it. Both are competitive, phased (feasibility →
development), and non-dilutive (grants/contracts, not equity).

**Q: So what does McNeese's role unlock?**
STTR eligibility. We can't pursue STTR without a research-institution partner. With
McNeese as that partner, a real slice of the funded research is performed at — and
paid to — the university, with your faculty and students on the funded work.

**Q: Which agencies / topics?** 🔴
Several agencies (DoD, DARPA, IARPA, NSF, DOE, NASA) fund trustworthy, efficient,
and autonomous-AI R&D that our work maps to. *[Fill in the specific agency/topic
you intend to target first — pick one concrete solicitation to make this real.]*

**Q: Have you won these contracts before?** 🔴
*[Answer with your actual SBIR/STTR history — awards, phases, agencies. If you're
early, say so honestly: "we're actively pursuing our first, and the STTR partner
requirement is the gap this partnership fills."]*

## Partnership mechanics

**Q: What exactly are you asking McNeese to commit?**
A faculty co-investigator, lab/equipment access, student researchers, the
research-institution partner role on proposals, and a letter of support / MOU. The
university's effort is scoped to its funded share — it's paid work, not a favor.

**Q: What's the time commitment for the professor?**
Co-investigator oversight on proposals and the university's work share — funded
through the awards. We do the heavy engineering; the faculty role is research
direction, oversight, and the scholarly output.

**Q: What's the risk / cost to McNeese?**
Low. No upfront cost. The university is funded for its work under any award; absent
an award, the commitment is a letter of support and scoping time. It's a standard
sponsored-research collaboration.

**Q: Who owns the intellectual property?** ⚠︎ 🔴
The core architecture is patent-pending on our side. SBIR/STTR have standard
data-rights frameworks (the small business generally retains IP with government-use
rights), and the small-business/research-institution IP split is set in the
partnership agreement. *[Confirm the exact posture you want to offer before the
meeting — this is the one place to be precise, not improvised.]*

**Q: What do you need from us to start?**
Pick one concrete SBIR/STTR topic to target together, and a letter of support /
MOU. From there we scope a first joint proposal.

## The skeptic's closer

**Q: Why should a university bet on a small team's AI project?** ⚠︎
Because we bring three things universities rarely get together: a *working,
distinctive* system (not a proposal-on-paper), a *funding channel the partnership
uniquely unlocks* (STTR), and *honest scope* about what's proven and what's next.
The downside is bounded; the upside is funded research, publications, and student
opportunity in a genuinely novel line of AI.
