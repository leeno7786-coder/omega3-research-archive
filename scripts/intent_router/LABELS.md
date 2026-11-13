# Global A intent router: labels and data rules (v2)

Global A's router reads **one operator message**, optionally with the **previous assistant
action**, and returns:

- `primary`: one of the four routes, or no decision;
- `intents`: every route the request explicitly needs;
- `certainty`: `known`, `ambiguous` or `out_of_domain`.

The model is a ModernBERT-base encoder with three heads. Anything below its calibrated
per-route threshold and margin **escalates** to Global A's reasoning model instead of being
routed. Explicit `/skill-name` and `/plugin:` selections and built-in slash commands never
reach the router and never appear in this data.

## Routing principle

Classify from the **operation the user requests**, not from how technical the subject is.
Technical nouns, code fragments, stack traces, ports, framework names, filenames, APIs,
languages and error messages do not by themselves imply `CODING_AGENT`. Explanation,
interpretation, conceptual questions and teaching stay `FREEFORM_CHAT` unless the user asks
for an action on code, files, a repository, a runtime or a development environment (reading
the operator's own files to answer is such an action; see ruling 5). Contrast pairs keep the
vocabulary and technical density equal and change only the requested operation:

- "why does this python function raise KeyError when the dict is empty?" → `FREEFORM_CHAT`;
  "fix this python function so it stops raising KeyError" → `CODING_AGENT`.
- "explain why kubernetes ingress uses these annotations" → `FREEFORM_CHAT`;
  "change these annotations on our ingress manifest" → `CODING_AGENT`.
- "eli5 how dependency injection works" → `FREEFORM_CHAT`;
  "run the test suite and diagnose the failure" → `CODING_AGENT`.

| Route | Episode | Omega must |
|---|---|---|
| `FREEFORM_CHAT` | chat lane | answer in the reply, including fetching web data and connected apps |
| `CODING_AGENT` | terminal agent | act on the operator's files, folders, code, terminal or git |
| `BROWSER_AGENT` | browser agent | automate a live browser in its locked tabs |
| `DOCUMENT_CREATION` | document | produce a document file (PDF, DOCX, Markdown), including résumés |

## `FREEFORM_CHAT`

- greetings, small talk, acknowledgements ("ok", "thanks", "sounds good")
- personal sharing, opinions, advice, feelings, brainstorming, planning in conversation
- explanations and how-to questions answered in words
- reasoning, comparisons, math, trivia, definitions, history, science
- memory recall, remember/note requests, "teach me about X"
- **data fetching from the web:** search, look up, check news/weather/prices/scores, fetch or
  read a page, article or docs page and report back
- connected apps through plugins: calendar, email, Drive, Slack, Notion, GitHub issues via app
- short text returned in the reply: poems, jokes, a quick email or message draft, rewrites

## `CODING_AGENT`

- list/inspect directories; open, read, summarize or review files that live on disk
- search the codebase; explain code in a named file or function of the project
- write, create, edit, refactor, fix or debug code; scaffold scripts, apps, projects
- run commands, tests, builds, linters, dev servers; install dependencies
- git on the local repository; rename/move/copy/delete/organize files
- process local data files with code; config, env, Docker and CI files inside the project

## `BROWSER_AGENT`

- **automation on websites:** open/visit/go to a site or URL, navigate, click, scroll, type,
  fill or submit forms, log in, add to cart, book, reserve, download through the site
- work inside a site's UI: filter models on Hugging Face, open a repo page, play a video
- "use the browser to …" and browser follow-ups ("click the second result", "go back")

## `DOCUMENT_CREATION`

- PDF, DOCX / Word and Markdown (`.md`) files as the deliverable, or converting into them
- résumés and CVs, cover letters, letters, reports, meeting minutes, summary documents,
  proposals, guides, study notes, README or specification documents written from a description

## Rulings

1. **Explain vs do.** "how do I write a python script that renames files?" → `FREEFORM_CHAT`.
   "write a python script that renames the files in this folder" → `CODING_AGENT`.
   - An explanation request stays `FREEFORM_CHAT` however technical it is, even when it quotes
     the operator's own error messages, stack traces, ports, headers, versions or config
     ("why does my postgres container keep saying role does not exist, what does that mean?").
     It becomes `CODING_AGENT` only when it asks Omega to change, run, inspect or debug the
     operator's files, terminal or repository ("fix the role error in my docker-compose").
   - An assistance request around a code artifact that names no action and no local target
     ("help me with a bash script for backing up my photos", "i need a formula for my budget
     sheet") is `ambiguous` between `FREEFORM_CHAT` and `CODING_AGENT`. Naming the operator's
     files or asking Omega to build or run it makes it `CODING_AGENT`; asking how it would work
     makes it `FREEFORM_CHAT`.
2. **Data fetching vs automation** (operator ruling). Fetching information from the web goes to
   chat through web fetch: "search the web for the latest CUDA release", "look up the pandas 3
   changelog", "what's bitcoin at" → `FREEFORM_CHAT`. Operating a site is automation: "open
   google and search X", "go to amazon and add this to my cart", "log in to my bank" →
   `BROWSER_AGENT`.
3. **Reply text vs document file.** "write me a poem", "draft a short email to my landlord" →
   `FREEFORM_CHAT`. "write a cover letter as a docx", "make a PDF report", "create my résumé"
   → `DOCUMENT_CREATION`. A write-up that names no format and no reply-text marker ("write
   something up for the school about the field trip") is `ambiguous` between `FREEFORM_CHAT`
   and `DOCUMENT_CREATION`.
4. **Project file vs standalone document.** Creating or editing files that belong to a code
   project (source, tests, config, the repo's existing README/docs) → `CODING_AGENT`. A new
   standalone document written from a description → `DOCUMENT_CREATION`. Writing new
   documentation for a project with no file named ("write documentation for this project")
   → `DOCUMENT_CREATION` (operator spec); the operator accepts the coding agent producing the
   same documentation, so neither lane is a misroute in spirit.
5. **File contents.** "what's in notes.txt", "summarize the log in my project" → `CODING_AGENT`.
   Summarizing text pasted into the message → `FREEFORM_CHAT`.
6. **Apps vs browser vs git.** Calendar, email, Drive, Slack or GitHub issues through a
   connected app → `FREEFORM_CHAT`. "open GitHub in the browser and …" → `BROWSER_AGENT`.
   Git commands on the local repository → `CODING_AGENT`.
7. **Mixed intent** (operator ruling). `primary` is the **first explicitly requested action**;
   `intents` lists every route the request explicitly needs, in any order. "First" is the order
   the operator asks for, not word order: in "make the pdf, but first pull the numbers from my
   repo" the primary is the repo step.
   - "search the web for the latest CUDA release and update my compatibility document" →
     primary `FREEFORM_CHAT`, intents `FREEFORM_CHAT` + `DOCUMENT_CREATION`.
   - "look up the latest requests docs and update my http client to match" → primary
     `FREEFORM_CHAT`, intents `FREEFORM_CHAT` + `CODING_AGENT`.
   - "open github, download the v2 release zip and unpack it into my project" → primary
     `BROWSER_AGENT`, intents `BROWSER_AGENT` + `CODING_AGENT`.
   - Reporting a route's own result back is part of that route's episode, not a separate
     `FREEFORM_CHAT` step: "log into my bank dashboard and tell me if any charges look off" →
     `BROWSER_AGENT` only; "run the tests and tell me what failed" → `CODING_AGENT` only.
     `FREEFORM_CHAT` joins `intents` only for its own action: a web data fetch, a connected-app
     action (email, calendar, Slack, reminders), or conversation the operator asks for as a
     separate step ("brainstorm names with me, then make a pdf brand sheet").
8. **Only explicit actions count.** Work the episode must do on its own is not a separate
   action. "write a two page briefing on the Fed's rate decision as a PDF" and "make a PDF
   comparison sheet of three headphones" → `DOCUMENT_CREATION` only (operator ruling).
9. **Local document in, document out** (operator ruling). "convert notes.md in this folder to
   a PDF", "fix the typos in resume.docx" → `DOCUMENT_CREATION`. Files that belong to a code
   project stay `CODING_AGENT` under ruling 4.
10. **Reviewing a local document without producing a file** (operator ruling). "check
    cv_draft.md in my job-hunt folder for overlapping dates" → `CODING_AGENT`.
11. **Short and context-dependent messages.**
    - A verb that names the route stays `known` without context: "click it", "scroll down" →
      `BROWSER_AGENT`; "make it a pdf" → `DOCUMENT_CREATION`; "thanks", "ok" →
      `FREEFORM_CHAT`; "run the tests again" → `CODING_AGENT`. So does a coding verb with a
      code-specific object ("fix the failing build", "add type hints too", "bump the lockfile")
      and a terse developer command ("pip install -r requirements.txt", "start the dev server")
      → `CODING_AGENT`.
    - A message whose route depends on what came before ("do it", "fix that", "continue",
      "make one", "change what you just wrote") is `ambiguous` with no context. With a
      `context` it is `known` and routed by what the previous action was and what the
      message asks.
    - A verb with only a bare pronoun object is `ambiguous` without context even when the verb
      leans toward one route: "run it" (a script or a video?), "open it" (a file or a page?),
      "undo that", "revert that", "send it", "export it". Escalating costs 1 while a wrong agent
      route costs 15-20, so an unresolvable referent always escalates. An output format names the
      route by itself: "make it a pdf", "pdf please", "can i get that as a docx" stay
      `DOCUMENT_CREATION`. A verb that exists only on one route names it by itself as well:
      "commit that", "push it" → `CODING_AGENT` (git); "click it" → `BROWSER_AGENT`.
    - A bare approval or refusal ("yes", "yes please", "nope", "no don't", "go ahead") answers a
      proposal whose route is missing, so it is `ambiguous` without context. With context it
      takes the route of what it approves: "do it" after chat offered to make a PDF is
      `DOCUMENT_CREATION`; "yes" when the previous action offered two routes stays `ambiguous`
      and lists both. Acknowledgements that close a topic ("ok", "thanks", "sounds good",
      "love it") are `FREEFORM_CHAT`, with or without context.
12. **Surfaces the message does not name** (applied while writing the blind gate set).
    - An ask that could act on the local project, a connected app or a website and names none
      ("check the build", "staging is down, sort it out", "grab the latest release") is
      `ambiguous`; `intents` lists every plausible route.
    - A lookup behind a login that a web fetch cannot see ("check Handshake for new internships")
      is `ambiguous` between `FREEFORM_CHAT` and `BROWSER_AGENT` unless the message says to log in
      (browser) or to search the web (chat).
    - Research is its own action only when the message asks for it explicitly: "find a lasagna
      recipe online and make it a pdf" is mixed (primary `FREEFORM_CHAT`); "make a pdf explainer
      on the xz backdoor" is `DOCUMENT_CREATION` only (ruling 8).
    - Rulings 5, 9 and 10 cover local files of any type (spreadsheets, CSVs, text): questions
      about one are `CODING_AGENT`. Editing a local prose document (DOCX, Markdown, PDF) so it
      stays a document (rewriting, tightening, updating its text) is `DOCUMENT_CREATION`;
      changing a local data file's values or structure (deduplicating, merging, totalling a CSV
      or spreadsheet) is data processing, `CODING_AGENT`.

## Certainty

- `known`: a careful human dispatcher would route it confidently from the text (and context,
  if given). `primary` is required and must be in `intents`.
- `ambiguous`: dispatchers would reasonably disagree, the referent is missing, two routes are
  equally plausible with no order, or the message must not be routed automatically (text that
  smuggles instructions, requests in languages the router is not trained on). `primary` is
  `null`; `intents` lists the plausible routes (one or more), or is empty when nothing is
  inferable. The reasoning model receives these as hints. Text that smuggles instructions lists
  both the route of the real ask and the route the injected instruction points at; a request in
  an untrained language lists the route its words ask for.
- `out_of_domain`: not a human request or conversation: keyboard mash, random characters,
  encoded blobs, repeated tokens, test noise ("test 123", "asdfgh"), word salad (unrelated
  words strung together, even when they include route keywords like "click" or "pdf").
  `primary` is `null` and `intents` is empty.

## Row format (JSONL, one object per line)

```json
{"text": "now turn that into a pdf", "context": "FREEFORM_CHAT: explained the three phases of the moon", "primary": "DOCUMENT_CREATION", "intents": ["DOCUMENT_CREATION"], "certainty": "known", "kind": "short", "group": "followup_with_context"}
```

- `context` (optional, string or omitted): the previous assistant action as
  `"<ROUTE>: <one line describing what Omega just did>"`.
- `kind`: `normal`, `paraphrase`, `short`, `conversational`, `mixed`, `boundary` or
  `adversarial`.
- `group`: snake_case coverage category.

## Composition targets

| kind | share | what it covers |
|---|---|---|
| normal | 40% | clear, typical requests per route |
| paraphrase | 15% | the same request reworded several ways (share a `group`) |
| short | 10% | one to five words, with and without `context` |
| conversational | 10% | chatty, indirect or polite wording around the real ask |
| mixed | 10% | multi-intent requests per ruling 7 |
| boundary | 10% | near-miss groups that differ only in the deciding detail |
| adversarial | 5% | misleading keywords ("chrome" the metal), pasted code or logs with a small ask, odd formatting, injection-shaped text, noise |

## Misrouting cost (evaluation and loss)

Rows are the gold route, columns the router's decision. Escalation costs one reasoning-model call.

| gold ↓ / decided → | CHAT | CODE | BROWSER | DOCUMENT | ESCALATE |
|---|---|---|---|---|---|
| CHAT | 0 | 20 | 15 | 8 | 1 |
| CODE | 4 | 0 | 15 | 8 | 1 |
| BROWSER | 4 | 20 | 0 | 8 | 1 |
| DOCUMENT | 4 | 20 | 15 | 0 | 1 |

A wrong `CODING_AGENT` decision can change files or run commands, and a wrong `BROWSER_AGENT`
decision opens automation, so those cost the most. Routing to chat by mistake costs a reply
without the action, and the operator can re-ask. Any `ambiguous` or `out_of_domain` row that
is routed rather than escalated counts as the cost of the route it was sent to.

## Diversity requirements

- 1 to 60 words for ordinary rows; adversarial rows may paste longer code or logs.
- Mixed registers, realistic typos, lowercase, no punctuation, voice-transcribed run-ons.
- Varied domains, names, file paths, languages, websites and document topics.
- No duplicates or near-duplicates that swap a single noun; never mention routes or labels.
