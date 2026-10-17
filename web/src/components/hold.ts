/** What a hold-to-confirm key says, from its label ("Decline"): "Hold to decline" at rest, and
 *  "Press again to decline" once a press has armed it (a click that came without a hold). */
export function holdWords(label: string) {
  const verb = label.toLowerCase();
  return { hold: `Hold to ${verb}`, armed: `Press again to ${verb}` };
}

/** Every line the key can show at rest, armed or just after letting go early (`bail`): it keeps
 *  room for the longest, so arming it never widens it or moves what's beside it. */
export function holdRoom(label: string, bail?: string) {
  const { hold, armed } = holdWords(label);
  return [hold, armed, ...(bail ? [bail] : [])];
}
