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
