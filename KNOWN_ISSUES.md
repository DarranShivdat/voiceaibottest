# Known Issues

Running list of bugs and design gaps to address. Newest at top.

## Missing contact info not handled
If a patient record (or new intake) has no phone and no email, the agent proceeds
without prompting for one, then still promises to "send a confirmation text/email"
it can't actually send.

Fix:
1. Require at least one contact method during intake — if both are missing, ask
   for one before advancing. (One channel is enough; some patients only have a phone.)
2. Make the confirmation promise conditional on what's available — only say "I'll
   text you" if there's a phone, "email you" if there's an email, and if neither
   exists, don't claim to send anything. The agent must not state an action it
   can't perform.

Note: "required fields" and "conditional confirmation actions" are config-layer
building blocks (different clinics require different fields), not one-off fixes.
Fold into the generality layer rather than hardcoding.

## STT mis-transcription of medical/domain terms
Deepgram mis-hears domain words — e.g. "Dr." transcribed as "drive". Higher risk
for a medical product: misheard provider/drug names mean the agent acts on wrong
data. Fix: Deepgram keyterm/keyword boosting (feed expected terms — provider
names, "Dr.", common drugs, clinic name). Per-clinic config, not a one-off.

## Turn detection over-sensitive to background noise
VAD triggers on background noise. Levers: VADParams confidence (toward 0.8),
stop_secs. Tune against real clinic-call recordings, not a quiet room.

<<<<<<< HEAD
## No API abuse protection (BLOCKER before any public hosting)
Bot runs with "Allowed origins: all (no restriction)", no auth, no rate limiting.
Fine locally (localhost only). NOT safe the moment it's on a public URL:
- /start can be spammed → each session burns paid Deepgram/Cartesia/Anthropic
  credits (cost abuse + can DoS the demo by exhausting provider limits).
- POST /api/flows/{name} (builder save) writes files to disk with no auth →
  anyone could overwrite flow configs or write arbitrary filenames.
Before hosting (ngrok or deploy), add at minimum:
1. Shared-secret / access token required to start a session and hit /api routes.
2. Rate limiting on /start and the API routes (N per IP per minute).
3. Filename validation on the save endpoint (no path traversal, restrict to flows/).
Do NOT share a public URL until at least the token gate is in place.
=======
## Builder can't edit function goto routing (KEY GAP for real configurability)
The builder edits node text, questions, "next", exposed functions, and initial
node — but NOT where a function routes (its goto). Most of the flow's real routing
(greeting->verify->intake etc.) lives in function gotos, so a GTM/clinic user
currently cannot rewire the flow through the UI — only add leaf nodes and edit
text. The "This node routes via its functions' goto — edit in the panel" message
is misleading (the panel has no goto editor). Next builder feature: expose
function routing (goto targets, conditional routes) as editable in the panel so
flows can actually be re-wired without editing JSON.
>>>>>>> t3code/9afe63c9

## Builder UX niceties (post-demo polish)
- Rename a step inline by double-clicking its name on the card (currently only
  editable in the side panel).
- Drag-select multiple nodes on the canvas (React Flow selectionOnDrag / multi-select).
