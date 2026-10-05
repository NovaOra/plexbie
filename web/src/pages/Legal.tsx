import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useTitle, PageHead } from "../components/ui";
import { SITE } from "../site";

// Plain-language policies for this household's Plexbie. Every install shows them,
// naming whoever runs it (SITE_OPERATOR, SITE_CONTACT), never the Plexbie project.
// Every statement here must stay true to what the bot and this site actually do;
// change the code and these together (PRODUCT.md).

const UPDATED = "October 4, 2026";
const WHO = SITE.operator || "its owner";

function Doc({ title, children }: { title: string; children: ReactNode }) {
  return (
    <article className="shell page legal">
      <PageHead as="header" title={title} lede={`Last updated ${UPDATED}`} />
      <div className="legal__body">{children}</div>
    </article>
  );
}

/** Where to ask: their email if they gave one, else the admins. */
const Ask = () => (SITE.contact ? <a href={`mailto:${SITE.contact}`}>{SITE.contact}</a> : <>an admin in the Discord server</>);

export function Privacy() {
  useTitle("Privacy");
  return (
    <Doc title="Privacy">
      <p>
        This website is a private, non-commercial service run by {WHO} for the members of one household and its
        private Discord server. It runs Plexbie, free software anyone can host for their own household. This page explains
        what this website handles, why, and what you can ask us to do with it. Questions go to <Ask />.
      </p>

      <h2>What we collect</h2>
      <ul>
        <li><b>Your Discord identity.</b> When you log in, Discord tells us your account ID, username, display name and avatar. We ask Discord for nothing else: not your email, your servers or your messages.</li>
        <li><b>Your Plex identity.</b> If you sign in with Plex, you type your password on plex.tv, never here. Plex tells us your account ID, username and email; we keep those in your login cookie and, if you join, in the bot’s member list. Plex also hands us a key for your account: we use it once, to check who you are and (with an invite link) to accept the invite for you, then sign Plexbie out of your Plex account so the key stops working. We never store it.</li>
        <li><b>Messages Plexbie sends you.</b> Each Discord message, website alert or email Plexbie sends you is kept for 90 days, with whether it arrived, so admins can see what you were told and help if something went missing.</li>
        <li><b>Invite links.</b> When an admin makes an invite link we keep the name they gave it, the email it’s locked to (if any), when it expires, and who used it. The link itself isn’t stored, only a fingerprint of it.</li>
        <li><b>Your join request.</b> If you ask to join, we keep the email address you type and the time you asked, so an admin can send the invitation and see that it was handled.</li>
        <li><b>Your requests.</b> Each request records what you asked for, when, and what happened to it, so you and the admins can follow it.</li>
        <li><b>Viewing activity.</b> The Plexbie bot already keeps watch-time totals, streaks, what members are watching right now, and when each account last watched. The website shows this to other logged-in members, as the Discord server does.</li>
        <li><b>Server logs.</b> Like most websites, our web server records basic request logs (IP address, time, page requested, browser type) for security and troubleshooting.</li>
      </ul>

      <h2>What we don’t do</h2>
      <ul>
        <li>No advertising, no tracking pixels, and no third-party scripts or embeds.</li>
        <li>We don’t sell or share your information. Nothing is used for marketing.</li>
        <li>Posters and covers are fetched by our server, so your browser does not contact image providers.</li>
      </ul>

      <h2>Cookies</h2>
      <p>
        The site sets one cookie that keeps you logged in, plus two short-lived ones: one while you’re signing in (10 minutes)
        and one when you open an invite link (an hour). All three are strictly necessary for the site to work, so we don’t ask
        for consent, and there are no others of ours. If this site is reached through Cloudflare, Cloudflare may add a
        short-lived security cookie of its own (__cf_bm) to tell people from bots. Logging out removes the login cookie;
        the rest expire on their own.
      </p>

      <h2>Who else is involved</h2>
      <ul>
        <li><b>The network in between.</b> If this site is reached through a service such as Cloudflare, that service carries its traffic and its own privacy policy applies to that.</li>
        <li><b>Discord</b> handles logging in. Its own privacy policy applies to your Discord account.</li>
        <li><b>Plex</b> handles “Sign in with Plex”, and receives your email only to share the server with you (when an admin approves a join request, or when you use an invite link). Plex’s privacy policy applies to your Plex account.</li>
        <li><b>Alerts.</b> If you turn on alerts, they reach you through your browser’s push service (Google, Apple or Mozilla) or, in the Plexbie app, through Expo and Google or Apple. They carry only the alert itself.</li>
        <li><b>Email.</b> If this server sends email, it goes through the email service its owner set up.</li>
      </ul>

      <h2>How long we keep it</h2>
      <p>
        Request history is kept while the service runs, because the bot uses it to manage what stays on the server.
        Join emails are kept so access can be managed, and both are deleted when you ask. Accounts that stop watching
        for 30 days are removed from the server by the bot.
      </p>

      <h2>Your choices</h2>
      <p>
        Wherever you live, you can ask to see the information we hold about you, have it corrected, or have it deleted.
        Ask <Ask />{SITE.contact ? " from an address we can match to you, or message an admin in the Discord server" : ""}.
        We’ll answer within 30 days. Deleting your information also ends your access.
      </p>

      <h2>Age</h2>
      <p>Plexbie follows Discord’s age rules. It isn’t meant for anyone under 13, and we don’t knowingly collect information from children.</p>

      <h2>Changes</h2>
      <p>If this page changes, the date at the top changes too, and anything significant is announced in the Discord server.</p>
    </Doc>
  );
}

export function Terms() {
  useTitle("Terms");
  return (
    <Doc title="Terms of use">
      <p>
        These terms cover this website, run by {WHO}. By logging in you agree to them. Questions go to <Ask />.
      </p>

      <h2>A private service</h2>
      <p>
        Plexbie is for the members of one household and its private Discord server. Access is a courtesy, not a contract: admins decide
        who gets in, and access can be paused or removed at any time, including automatically when an account goes
        30 days without watching anything.
      </p>

      <h2>It’s free</h2>
      <p>Plexbie costs nothing. There is nothing to buy, no subscription and nothing to refund.</p>

      <h2>Fair use</h2>
      <ul>
        <li>Keep your login to yourself and don’t share access.</li>
        <li>Don’t try to break, overload or get around the site or the bot.</li>
        <li>Requests are suggestions. Admins approve or decline each one, and approval doesn’t guarantee anything gets added.</li>
        <li>Follow the Discord server’s rules and Discord’s and Plex’s own terms.</li>
      </ul>

      <h2>No guarantees</h2>
      <p>
        This site is run by {WHO} in their spare time and is provided as it is. It may be slow, wrong or offline, and
        we’re not liable for any loss from using it, as far as the law allows.
      </p>

      <h2>Credits</h2>
      <p>
        Film and TV information and artwork come from TMDB. This product uses the TMDB API but is not endorsed or
        certified by TMDB. Book information and covers come from Open Library. Posters and covers belong to their
        respective owners. The Plexbie name and logo belong to the Plexbie project (plexbie.com).
      </p>

      <h2>Changes</h2>
      <p>If these terms change, the date at the top changes too. See also the <Link to="/privacy">privacy policy</Link>.</p>
    </Doc>
  );
}
